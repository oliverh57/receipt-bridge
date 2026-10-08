// receipt-reader — on-device OCR for Receipt Bridge (PLAN.md §5.1).
//
// Reads a receipt photo (HEIC, JPEG, PNG, WebP) or PDF and prints JSON:
// the text regrouped into rows, when and where the photo was taken, and
// optionally a JPEG copy sized for FreeAgent. Nothing leaves the Mac.
//
//   receipt-reader <file> [--jpeg <out.jpg>] [--supplier] [--pages <dir>]
//                         [--clean <prefix>]
//
// --pages writes each page of a PDF as page-1.jpg, page-2.jpg… (for showing
// it with what was read highlighted).
//
// --clean (photos only) also writes tidied copies: <prefix>.crop.jpg (cut to
// the receipt's edges, squared up, grey with the contrast lifted) and
// <prefix>.enhance.jpg (straightened and contrast only, nothing cut away).
// Each is read again so app/photo_inbox.py can check nothing was lost before
// using it. The crop is only attempted when no text sits on or near the
// receipt's edge; when in doubt it isn't made.
//
// Compiled on demand by app/receipt_reader.py; not part of the .app bundle.
//
// Money, VAT and dates are NOT read here. That's app/receipt_text.py's job,
// with rules that were tested against real receipts. The on-device language
// model is only asked for a supplier name (--supplier), because on money it
// fabricated figures.

import CoreGraphics
import CoreImage
import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision
#if canImport(FoundationModels)
import FoundationModels
#endif

struct Output: Encodable {
    var rows: [String] = []
    var pages = 0
    var width = 0
    var height = 0
    var skew_degrees = 0.0
    var photo_taken: String? = nil      // EXIF DateTimeOriginal, local time as printed
    var latitude: Double? = nil
    var longitude: Double? = nil
    var jpeg: String? = nil
    var supplier_guess: String? = nil
    // Where each row's pieces sit: page, then [x, y, w, h] as fractions of
    // the upright page (top-left origin), for highlighting what was read.
    var layout: [RowLayout] = []
    var page_images: [String] = []
    var model: String = "not asked"
    var clean: [CleanCandidate] = []
    var clean_note: String? = nil       // why no crop was offered
}

struct CleanCandidate: Encodable {
    let kind: String                    // "crop" or "enhance"
    let path: String
    let steps: [String]                 // what was done, in plain words
    let rows: [String]
    let layout: [RowLayout]
    let dropped: [String]               // background text the crop cut away
}

struct PieceBox: Encodable { let t: String; let b: [Double] }
struct RowLayout: Encodable { let page: Int; let pieces: [PieceBox] }

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

// ---- input --------------------------------------------------------------------

let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage: receipt-reader <file> [--jpeg <out.jpg>] [--supplier]") }
let inputURL = URL(fileURLWithPath: args[1])
var jpegOut: URL? = nil
if let i = args.firstIndex(of: "--jpeg"), i + 1 < args.count { jpegOut = URL(fileURLWithPath: args[i + 1]) }
let wantSupplier = args.contains("--supplier")
var pagesOut: URL? = nil
if let i = args.firstIndex(of: "--pages"), i + 1 < args.count { pagesOut = URL(fileURLWithPath: args[i + 1], isDirectory: true) }
var cleanOut: String? = nil
if let i = args.firstIndex(of: "--clean"), i + 1 < args.count { cleanOut = args[i + 1] }

var out = Output()

// Pages to read, each with the orientation Vision should apply.
var pages: [(CGImage, CGImagePropertyOrientation)] = []
// A photo turned upright at FreeAgent size: what --jpeg writes, and what
// --clean starts from.
var uprightCopy: CGImage? = nil

func writeJPEG(_ image: CGImage, to target: URL, quality: Double = 0.8) -> Bool {
    guard let dest = CGImageDestinationCreateWithURL(target as CFURL, UTType.jpeg.identifier as CFString, 1, nil)
    else { return false }
    CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
    return CGImageDestinationFinalize(dest)
}

if inputURL.pathExtension.lowercased() == "pdf" {
    guard let pdf = CGPDFDocument(inputURL as CFURL) else { fail("cannot open PDF") }
    let scale: CGFloat = 200.0 / 72.0          // render at 200 dpi
    for n in 1...max(pdf.numberOfPages, 1) {
        guard let page = pdf.page(at: n) else { continue }
        let box = page.getBoxRect(.mediaBox)
        let w = Int(box.width * scale), h = Int(box.height * scale)
        guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { continue }
        ctx.setFillColor(CGColor(gray: 1, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
        ctx.scaleBy(x: scale, y: scale)
        ctx.drawPDFPage(page)
        if let image = ctx.makeImage() { pages.append((image, .up)) }
    }
} else {
    guard let source = CGImageSourceCreateWithURL(inputURL as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { fail("cannot open image") }
    let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any] ?? [:]
    let raw = (props[kCGImagePropertyOrientation] as? UInt32) ?? 1
    pages.append((image, CGImagePropertyOrientation(rawValue: raw) ?? .up))

    if let exif = props[kCGImagePropertyExifDictionary] as? [CFString: Any],
       let taken = exif[kCGImagePropertyExifDateTimeOriginal] as? String {
        out.photo_taken = taken                  // "2025:08:03 08:31:12"
    }
    if let gps = props[kCGImagePropertyGPSDictionary] as? [CFString: Any],
       let lat = gps[kCGImagePropertyGPSLatitude] as? Double,
       let lon = gps[kCGImagePropertyGPSLongitude] as? Double {
        out.latitude = (gps[kCGImagePropertyGPSLatitudeRef] as? String) == "S" ? -lat : lat
        out.longitude = (gps[kCGImagePropertyGPSLongitudeRef] as? String) == "W" ? -lon : lon
    }

    // A JPEG copy for FreeAgent: upright, longest edge ≤ 2,400 px, well
    // under its 5 MB limit. Also turns HEIC into something it accepts.
    if jpegOut != nil || cleanOut != nil {
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: 2400,
        ]
        guard let small = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
        else { fail("cannot write JPEG") }
        uprightCopy = small
        if let target = jpegOut {
            guard writeJPEG(small, to: target) else { fail("cannot write JPEG") }
            out.jpeg = target.path
        }
    }
}
guard !pages.isEmpty else { fail("nothing to read") }
out.pages = pages.count

// Page images for the app (PDFs only: a photo already has its JPEG).
if let dir = pagesOut, inputURL.pathExtension.lowercased() == "pdf" {
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    for (i, (image, _)) in pages.enumerated() {
        let target = dir.appendingPathComponent("page-\(i + 1).jpg")
        guard let dest = CGImageDestinationCreateWithURL(target as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else { continue }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: 0.75] as CFDictionary)
        if CGImageDestinationFinalize(dest) { out.page_images.append(target.path) }
    }
}

// ---- OCR, rows rebuilt along the text's own slope ---------------------------------

struct Piece { let text: String; let x: Double; let y: Double; let height: Double; let box: CGRect }

struct PageReading { var rows: [String]; var layout: [RowLayout]; var skew: Double; var width: Int; var height: Int }

func readPage(_ image: CGImage, _ orientation: CGImagePropertyOrientation, page: Int) throws -> PageReading {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.automaticallyDetectsLanguage = true
    request.recognitionLanguages = ["en-GB", "ja-JP", "de-DE", "fr-FR", "it-IT", "es-ES", "pt-PT", "nl-NL"]
    try VNImageRequestHandler(cgImage: image, orientation: orientation).perform([request])
    let observations = request.results ?? []

    // Work in pixels, so the slope isn't distorted by the image's aspect ratio.
    let upright = [.left, .right, .leftMirrored, .rightMirrored].contains(orientation)
    let W = Double(upright ? image.height : image.width)
    let H = Double(upright ? image.width : image.height)

    // The page's tilt: the median slope of each text line's top edge.
    var slopes: [Double] = []
    for o in observations where (o.topRight.x - o.topLeft.x) * W > 40 {
        slopes.append(atan2((o.topRight.y - o.topLeft.y) * H, (o.topRight.x - o.topLeft.x) * W))
    }
    slopes.sort()
    let angle = slopes.isEmpty ? 0 : slopes[slopes.count / 2]

    // Rotate every line's centre by -angle, so a tilted photo's lines become
    // horizontal before grouping into rows.
    let c = cos(-angle), s = sin(-angle)
    var pieces: [Piece] = []
    for o in observations {
        guard let text = o.topCandidates(1).first?.string else { continue }
        let cx = o.boundingBox.midX * W, cy = o.boundingBox.midY * H
        pieces.append(Piece(text: text, x: cx * c - cy * s, y: cx * s + cy * c,
                            height: o.boundingBox.height * H, box: o.boundingBox))
    }
    let heights = pieces.map(\.height).sorted()
    let lineHeight = heights.isEmpty ? 1 : heights[heights.count / 2]

    // Top to bottom; a piece joins the current row when its centre is within
    // 0.4 of a line height of the row's first piece.
    pieces.sort { $0.y > $1.y }
    var rows: [[Piece]] = []
    for p in pieces {
        if let first = rows.last?.first, abs(first.y - p.y) < 0.4 * lineHeight {
            rows[rows.count - 1].append(p)
        } else {
            rows.append([p])
        }
    }
    let ordered = rows.map { $0.sorted { $0.x < $1.x } }
    let r4 = { (v: Double) in (v * 10000).rounded() / 10000 }
    // Vision's boxes are normalised with the origin bottom-left
    let layout = ordered.map { row in
        RowLayout(page: page, pieces: row.map {
            PieceBox(t: $0.text, b: [r4($0.box.minX), r4(1 - $0.box.maxY), r4($0.box.width), r4($0.box.height)])
        })
    }
    return PageReading(rows: ordered.map { $0.map(\.text).joined(separator: "   ") }, layout: layout,
                       skew: (angle * 180 / .pi * 10).rounded() / 10, width: Int(W), height: Int(H))
}

// ---- tidying a photo ------------------------------------------------------------------
//
// Two copies, each read again by the caller before it's used:
//   crop     cut to the four corners Vision finds for the receipt, squared up
//   enhance  the whole photo, straightened, nothing cut away
// Both are grey, with the paper evened out to white and the print darkened
// on a curve (not a black/white threshold, which wipes out faded print).
//
// A crop is only made when Vision is sure of the edges AND no text lies on
// or near them. Text crossing an edge means the receipt carries on past the
// edge Vision found (a white receipt on a white desk); cutting there would
// lose it. Text well away from the receipt (a sign behind it, a phone's
// buttons) may be cut away, and is listed so the caller can say so.

// Filters work on the values as seen (sRGB's curve), not linear light: the
// tone curve below is drawn for what the eye sees.
let ciContext = CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])

func cross(_ o: CGPoint, _ a: CGPoint, _ b: CGPoint) -> CGFloat {
    (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x)
}

func insideConvex(_ p: CGPoint, _ poly: [CGPoint]) -> Bool {
    var sign: CGFloat = 0
    for i in 0..<poly.count {
        let c = cross(poly[i], poly[(i + 1) % poly.count], p)
        if c == 0 { continue }
        if sign == 0 { sign = c } else if (c > 0) != (sign > 0) { return false }
    }
    return true
}

func distanceToSegment(_ p: CGPoint, _ a: CGPoint, _ b: CGPoint) -> CGFloat {
    let dx = b.x - a.x, dy = b.y - a.y
    let len2 = dx * dx + dy * dy
    let t = len2 == 0 ? 0 : max(0, min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2))
    return hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))
}

func segmentsCross(_ a: CGPoint, _ b: CGPoint, _ c: CGPoint, _ d: CGPoint) -> Bool {
    (cross(a, b, c) > 0) != (cross(a, b, d) > 0) && (cross(c, d, a) > 0) != (cross(c, d, b) > 0)
}

/// Gap between two convex polygons; 0 when they touch or overlap.
func gap(_ p: [CGPoint], _ q: [CGPoint]) -> CGFloat {
    if p.contains(where: { insideConvex($0, q) }) || q.contains(where: { insideConvex($0, p) }) { return 0 }
    var best = CGFloat.infinity
    for i in 0..<p.count {
        let a = p[i], b = p[(i + 1) % p.count]
        for j in 0..<q.count {
            let c = q[j], d = q[(j + 1) % q.count]
            if segmentsCross(a, b, c, d) { return 0 }
            best = min(best, distanceToSegment(a, c, d), distanceToSegment(c, a, b))
        }
    }
    return best
}

func interiorAnglesDegrees(_ poly: [CGPoint]) -> [Double] {
    (0..<poly.count).map { i in
        let p = poly[(i + poly.count - 1) % poly.count], v = poly[i], n = poly[(i + 1) % poly.count]
        let a = atan2(Double(p.y - v.y), Double(p.x - v.x)), b = atan2(Double(n.y - v.y), Double(n.x - v.x))
        var d = abs(a - b) * 180 / .pi
        if d > 180 { d = 360 - d }
        return d
    }
}

/// Grey, paper evened out to white, print darkened.
func tidy(_ image: CIImage) -> CIImage {
    let e = image.extent
    let grey = image.applyingFilter("CIColorControls", parameters: [kCIInputSaturationKey: 0])
    // The paper's own brightness with the print taken out (the brightest
    // value nearby, blurred). Dividing by it lifts shadows and turns the
    // paper white without changing the print's shape.
    let r = max(e.width, e.height) / 90
    let paper = grey.clampedToExtent()
        .applyingFilter("CIMorphologyMaximum", parameters: [kCIInputRadiusKey: r])
        .applyingFilter("CIGaussianBlur", parameters: [kCIInputRadiusKey: r * 2])
        .cropped(to: e)
        // Never brighten more than ~2x: a dark background (a hand, a table)
        // isn't paper, and lifting it only shows up noise.
        .applyingFilter("CIColorClamp", parameters: ["inputMinComponents": CIVector(x: 0.5, y: 0.5, z: 0.5, w: 0)])
    let flat = paper.applyingFilter("CIDivideBlendMode", parameters: [kCIInputBackgroundImageKey: grey])
    // Gentle: the print darkened, light greys (print showing through from
    // the back, the soft edges of thin print) left alone. Pushing those to
    // white thinned the print until OCR misread it; tested on 52 photos.
    return flat.applyingFilter("CIToneCurve", parameters: [
        "inputPoint0": CIVector(x: 0, y: 0),
        "inputPoint1": CIVector(x: 0.3, y: 0.12),
        "inputPoint2": CIVector(x: 0.6, y: 0.45),
        "inputPoint3": CIVector(x: 0.85, y: 0.85),
        "inputPoint4": CIVector(x: 1, y: 1),
    ]).cropped(to: e)
}

func render(_ image: CIImage) -> CGImage? {
    let e = image.extent.integral
    let moved = image.transformed(by: CGAffineTransform(translationX: -e.minX, y: -e.minY))
    return ciContext.createCGImage(moved, from: CGRect(origin: .zero, size: e.size),
                                   format: .L8, colorSpace: CGColorSpace(name: CGColorSpace.genericGrayGamma2_2))
}

func cleanCopies(_ upright: CGImage, layout: [RowLayout], skewDegrees: Double,
                 prefix: String) -> (made: [CleanCandidate], note: String?) {
    let W = CGFloat(upright.width), H = CGFloat(upright.height)
    // Every piece of text read from the photo, as a box in pixels (top-left origin).
    let boxes: [(String, [CGPoint])] = layout.filter { $0.page == 0 }.flatMap(\.pieces).map { piece in
        let x = piece.b[0] * W, y = piece.b[1] * H, w = piece.b[2] * W, h = piece.b[3] * H
        return (piece.t, [CGPoint(x: x, y: y), CGPoint(x: x + w, y: y),
                          CGPoint(x: x + w, y: y + h), CGPoint(x: x, y: y + h)])
    }
    if boxes.count < 3 { return ([], "no text to check a tidied copy against") }
    let heights = boxes.map { $0.1[2].y - $0.1[0].y }.sorted()
    let lineHeight = heights[heights.count / 2]

    var made: [CleanCandidate] = []
    var note: String? = nil
    let source = CIImage(cgImage: upright)

    func keep(_ kind: String, _ image: CIImage, steps: [String], dropped: [String]) {
        guard let cg = render(image) else { return }
        let target = URL(fileURLWithPath: "\(prefix).\(kind).jpg")
        guard writeJPEG(cg, to: target, quality: 0.85),
              let read = try? readPage(cg, .up, page: 0) else { return }
        made.append(CleanCandidate(kind: kind, path: target.path, steps: steps,
                                   rows: read.rows, layout: read.layout, dropped: dropped))
    }

    // -- crop --
    let request = VNDetectDocumentSegmentationRequest()
    let found = (try? VNImageRequestHandler(cgImage: upright, options: [:]).perform([request])) != nil
        ? request.results?.first : nil
    if let doc = found {
        // Vision: normalised, origin bottom-left. Here: pixels, origin top-left.
        let px = { (p: CGPoint) in CGPoint(x: p.x * W, y: (1 - p.y) * H) }
        let raw = [doc.topLeft, doc.topRight, doc.bottomRight, doc.bottomLeft].map(px)
        // Shoelace formula, written out: as one chained expression some Swift
        // compilers gave up type-checking it, and the reader never got built.
        var twiceArea: CGFloat = 0
        for i in 0..<raw.count {
            let a: CGPoint = raw[i]
            let b: CGPoint = raw[(i + 1) % raw.count]
            twiceArea += a.x * b.y - b.x * a.y
        }
        let area: CGFloat = abs(twiceArea) / 2
        let angles = interiorAnglesDegrees(raw)
        // Pushed out a little, so the crop never shaves the print at the edge.
        let margin = max(lineHeight * 0.8, min(W, H) * 0.012)
        let sumX: CGFloat = raw.reduce(0) { $0 + $1.x }
        let sumY: CGFloat = raw.reduce(0) { $0 + $1.y }
        let centre = CGPoint(x: sumX / 4, y: sumY / 4)
        let quad = raw.map { v -> CGPoint in
            let d = hypot(v.x - centre.x, v.y - centre.y)
            let k = d == 0 ? 0 : margin * 1.4 / d
            return CGPoint(x: min(max(v.x + (v.x - centre.x) * k, 0), W),
                           y: min(max(v.y + (v.y - centre.y) * k, 0), H))
        }
        var inside = 0
        var touching: [String] = []
        var dropped: [String] = []
        for (text, box) in boxes {
            if box.allSatisfy({ insideConvex($0, quad) }) { inside += 1 }
            else if gap(box, quad) < lineHeight * 2.5 { touching.append(text) }
            else { dropped.append(text) }
        }
        let conf = String(format: "%.2f", doc.confidence)
        if doc.confidence < 0.6 {
            note = "receipt edges unclear (confidence \(conf))"
        } else if area < W * H * 0.12 {
            note = "the receipt found is too small a part of the photo"
        } else if angles.contains(where: { $0 < 60 || $0 > 120 }) {
            note = "the receipt's outline found isn't a rectangle"
        } else if !touching.isEmpty {
            note = "text runs to the receipt's edge (\(touching.prefix(3).joined(separator: ", ")))"
        } else if inside < 3 || Double(dropped.count) > Double(boxes.count) * 0.25 {
            note = "most of the text is outside the receipt found"
        } else {
            // Core Image: pixels, origin bottom-left.
            let ci = { (p: CGPoint) in CIVector(x: p.x, y: H - p.y) }
            let squared = source.applyingFilter("CIPerspectiveCorrection", parameters: [
                "inputTopLeft": ci(quad[0]), "inputTopRight": ci(quad[1]),
                "inputBottomRight": ci(quad[2]), "inputBottomLeft": ci(quad[3]),
            ])
            keep("crop", tidy(squared), steps: ["cropped to the receipt", "squared up", "grey, contrast lifted"],
                 dropped: dropped)
        }
    } else {
        note = "no receipt edges found"
    }

    // -- enhance: nothing cut away; tilt taken out by turning on a larger white canvas --
    var image = tidy(source)
    var steps = ["grey, contrast lifted"]
    if abs(skewDegrees) >= 0.7 {
        let e = image.extent
        let turn = CGAffineTransform(translationX: e.midX, y: e.midY)
            .rotated(by: CGFloat(-skewDegrees * .pi / 180))
            .translatedBy(x: -e.midX, y: -e.midY)
        let turned = image.transformed(by: turn)
        image = turned.composited(over: CIImage(color: .white).cropped(to: turned.extent)).cropped(to: turned.extent)
        steps.insert(String(format: "straightened %.1f°", abs(skewDegrees)), at: 0)
    }
    keep("enhance", image, steps: steps, dropped: [])
    return (made, note)
}

do {
    for (i, (image, orientation)) in pages.enumerated() {
        let read = try readPage(image, orientation, page: i)
        if i == 0 { out.width = read.width; out.height = read.height }
        out.rows += read.rows
        out.layout += read.layout
        out.skew_degrees = read.skew
    }
} catch {
    fail("OCR failed: \(error)")
}

// ---- tidied copies (optional) -------------------------------------------------------

if let prefix = cleanOut, let upright = uprightCopy {
    let candidates = cleanCopies(upright, layout: out.layout, skewDegrees: out.skew_degrees, prefix: prefix)
    out.clean = candidates.made
    out.clean_note = candidates.note
}

// ---- supplier name (optional) -----------------------------------------------------

#if canImport(FoundationModels)
@available(macOS 26.0, *)
@Generable
struct Supplier {
    @Guide(description: "The trading name of the shop, restaurant or company that issued the receipt, as printed. Not a street, station, airport, card scheme or payment processor.")
    var name: String
}
#endif

// No text, nothing to name: asked anyway, the model invents one ("Cafe 101"
// for a photo of a thumbs-up).
if wantSupplier && out.rows.count < 3 {
    out.model = "not asked: no text on the photo"
} else if wantSupplier {
    #if canImport(FoundationModels)
    if #available(macOS 26.0, *) {
        switch SystemLanguageModel.default.availability {
        case .available:
            let session = LanguageModelSession(instructions: "You name the business that issued a receipt. Use only text that is printed on it.")
            let semaphore = DispatchSemaphore(value: 0)
            let prompt = out.rows.prefix(60).joined(separator: "\n")
            Task {
                do {
                    let answer = try await session.respond(to: "Receipt text:\n\(prompt)", generating: Supplier.self)
                    out.supplier_guess = answer.content.name
                    out.model = "available"
                } catch {
                    out.model = "declined: \(error)"     // e.g. unsupported language
                }
                semaphore.signal()
            }
            semaphore.wait()
        case .unavailable(let reason):
            out.model = "unavailable: \(reason)"
        }
    } else {
        out.model = "unavailable: needs macOS 26"
    }
    #else
    out.model = "unavailable: no FoundationModels"
    #endif
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys]
FileHandle.standardOutput.write(try encoder.encode(out))
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
