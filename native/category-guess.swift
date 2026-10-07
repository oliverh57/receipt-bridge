// category-guess — suggest a FreeAgent category for receipts, on this Mac
// (PLAN.md §11, "AI guess"). Nothing leaves the Mac.
//
//   category-guess < request.json > answer.json
//
// Request: {"categories": ["Travel", …], "receipts": [{"id": 1, "supplier":
// "Trainline", "text": "Off-peak return …"}, …]}
// Answer:  {"model": "available", "guesses": {"1": "Travel", …}}
//
// The model can only answer with one of the names it was given (a schema of
// choices), so it can't invent a category. It's a suggestion: the receipt
// still waits for you, tagged "AI guess". Compiled on demand by
// app/category_guess.py; not part of the .app bundle.

import Foundation
#if canImport(FoundationModels)
import FoundationModels
#endif

struct Item: Decodable { let id: Int; let supplier: String; let text: String }
struct Request: Decodable { let categories: [String]; let receipts: [Item] }
struct Answer: Encodable { var model = "not asked"; var guesses: [String: String] = [:] }

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

// Generic bookkeeping sense, not anyone's own suppliers.
let guidance = """
You pick the bookkeeping category for one purchase by a UK sole trader or small company, \
judging by the supplier and what the receipt shows was bought.
- Food and drink (restaurants, cafés, takeaways, supermarket food) and hotels: the meals or accommodation category.
- Trains, taxis, ride hailing, buses, flights, parking, fuel for travel: the travel category.
- Online services, apps, AI tools, cloud services and subscriptions: the software category, not hardware.
- Phone, mobile data, eSIMs and broadband: the telephone or internet category.
- "Cost of Sales", "Materials" and similar are only for goods the business resells or turns into products; \
they are rarely right for an everyday receipt.
"""

let input = FileHandle.standardInput.readDataToEndOfFile()
guard let request = try? JSONDecoder().decode(Request.self, from: input) else { fail("bad request") }
var answer = Answer()

#if canImport(FoundationModels)
if #available(macOS 26.0, *), !request.categories.isEmpty {
    switch SystemLanguageModel.default.availability {
    case .available:
        answer.model = "available"
        let choice = DynamicGenerationSchema(name: "Category", anyOf: request.categories)
        let root = DynamicGenerationSchema(name: "Answer", properties: [
            DynamicGenerationSchema.Property(name: "category", schema: choice)])
        guard let schema = try? GenerationSchema(root: root, dependencies: [choice]) else { fail("bad schema") }
        let semaphore = DispatchSemaphore(value: 0)
        Task {
            for item in request.receipts {
                // a fresh session each time, so one receipt can't colour the next
                let session = LanguageModelSession(instructions: guidance)
                let prompt = "Supplier: \(item.supplier)\nReceipt:\n\(item.text.prefix(1500))"
                do {
                    let reply = try await session.respond(to: prompt, schema: schema)
                    answer.guesses[String(item.id)] = try reply.content.value(String.self, forProperty: "category")
                } catch {
                    continue          // e.g. an unsupported language: no guess for this one
                }
            }
            semaphore.signal()
        }
        semaphore.wait()
    case .unavailable(let reason):
        answer.model = "unavailable: \(reason)"
    }
} else {
    answer.model = "unavailable: needs macOS 26"
}
#else
answer.model = "unavailable: no FoundationModels"
#endif

let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys]
FileHandle.standardOutput.write(try encoder.encode(answer))
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
