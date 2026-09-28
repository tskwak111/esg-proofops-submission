import Foundation
import Vision
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
let supported = try request.supportedRecognitionLanguages()
let languages = ["ko-KR", "en-US"]
guard languages.allSatisfy({ supported.contains($0) }) else {
    throw NSError(domain: "UnsupportedOCRLanguages", code: 1)
}
request.recognitionLanguages = languages
try VNImageRequestHandler(url: URL(fileURLWithPath: CommandLine.arguments[1])).perform([request])
let result: [String: Any] = [
    "reader": "Apple Vision",
    "revision": request.revision,
    "os": ProcessInfo.processInfo.operatingSystemVersionString,
    "languages": languages,
    "text": (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
]
let bytes = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
print(String(data: bytes, encoding: .utf8)!)
