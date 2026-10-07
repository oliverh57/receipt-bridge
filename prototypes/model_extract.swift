import Foundation
import Vision
import FoundationModels

@Generable
struct Receipt {
    @Guide(description: "Trading name of the business that issued the receipt")
    var supplier: String
    @Guide(description: "Supplier's VAT registration number exactly as printed, or empty if none")
    var supplierVATNumber: String
    @Guide(description: "Date of payment as YYYY-MM-DD")
    var date: String
    @Guide(description: "Amount before VAT")
    var net: Double
    @Guide(description: "VAT amount; 0 if none shown")
    var vat: Double
    @Guide(description: "Total paid including VAT")
    var gross: Double
    @Guide(description: "VAT rate as a percentage, e.g. 20, 5 or 0")
    var vatRate: Double
}

// OCR, then rebuild rows by vertical position so labels sit next to their values.
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
try VNImageRequestHandler(url: URL(fileURLWithPath: CommandLine.arguments[1])).perform([req])
let obs = (req.results ?? []).compactMap { o -> (CGRect, String)? in
    guard let s = o.topCandidates(1).first?.string else { return nil }
    return (o.boundingBox, s)
}.sorted { $0.0.midY > $1.0.midY }
var rows: [[(CGRect, String)]] = []
for o in obs {
    if let last = rows.last?.first, abs(last.0.midY - o.0.midY) < 0.008 { rows[rows.count - 1].append(o) }
    else { rows.append([o]) }
}
let text = rows.map { $0.sorted { $0.0.minX < $1.0.minX }.map(\.1).joined(separator: "   ") }.joined(separator: "\n")

let t0 = Date()
let session = LanguageModelSession(instructions: "You extract fields from UK receipts. Use only what is printed; never invent values.")
let r = try await session.respond(to: "Receipt text:\n\(text)", generating: Receipt.self)
print("on-device model: \(String(format: "%.1f", Date().timeIntervalSince(t0)))s")
let c = r.content
print("supplier=\(c.supplier) | vatNo=\(c.supplierVATNumber) | date=\(c.date) | net=\(c.net) | vat=\(c.vat) | gross=\(c.gross) | rate=\(c.vatRate)")
