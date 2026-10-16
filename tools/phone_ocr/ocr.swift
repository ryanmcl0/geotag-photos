// Apple Vision OCR over a list of images, for tools/phone_text_scan.py.
//
// usage: ocr <listfile>      one "key<TAB>path" per line
// prints one JSON line per image: {"k": key, "r": [{"t": text, "c": confidence, "h": box height}]}
// Recognises Simplified Chinese and English in one pass (accurate mode; the fast
// mode is Latin-only). Images that fail to decode print an empty result.
import Foundation
import Vision
import ImageIO

let lines = try! String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8).split(separator: "\n")
for line in lines {
    let parts = line.split(separator: "\t", maxSplits: 1).map(String.init)
    if parts.count < 2 { continue }
    let key = parts[0], path = parts[1]
    autoreleasepool {
        var out: [[String: Any]] = []
        if let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
           let img = CGImageSourceCreateImageAtIndex(src, 0, nil) {
            let req = VNRecognizeTextRequest()
            req.recognitionLevel = .accurate
            req.recognitionLanguages = ["zh-Hans", "en-US"]
            req.usesLanguageCorrection = false  // keep the misspellings: they are the point
            try? VNImageRequestHandler(cgImage: img, options: [:]).perform([req])
            for o in req.results ?? [] {
                if let c = o.topCandidates(1).first {
                    out.append(["t": c.string, "c": Double(c.confidence), "h": Double(o.boundingBox.height)])
                }
            }
        }
        let d = try! JSONSerialization.data(withJSONObject: ["k": key, "r": out])
        print(String(data: d, encoding: .utf8)!)
        fflush(stdout)
    }
}
