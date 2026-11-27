import AppKit
import CoreImage
import SwiftUI

/// The desktop's answer to mail_web_enrol: the phone page's one-time enrolment address and when it lapses.
struct PhoneEnrolment: Decodable, Equatable {
    let url: String
    let lapses: Double
}

/// A QR code of a text, drawn crisp at `scale` pixels a module (CoreImage's CIQRCodeGenerator, error correction M).
enum PhoneQR {
    static func image(_ text: String, scale: CGFloat) -> NSImage? {
        guard let filter = CIFilter(name: "CIQRCodeGenerator") else { return nil }
        filter.setValue(Data(text.utf8), forKey: "inputMessage")
        filter.setValue("M", forKey: "inputCorrectionLevel")
        guard let code = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: scale, y: scale)) else { return nil }
        let rep = NSCIImageRep(ciImage: code)
        let image = NSImage(size: rep.size)
        image.addRepresentation(rep)
        return image
    }
}

/// The phone sheet (the menu's Phone…): a one-time QR code that enrols the owner's phone for the Agent Mail page, which
/// the desktop serves to the owner's tailnet alone (README, "Mail on the phone"), and signing the phone out or
/// forgetting it.
struct PhoneSheet: View {
    @ObservedObject var model: BoardModel
    @State private var enrolment: PhoneEnrolment?
    @State private var said: String?
    @State private var busy = false
    @State private var confirmForget = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Agent mail on your phone").font(.headline)
            Text("Scan the code with your phone's camera and open the link, then touch the fingerprint sensor. "
                 + "Only your phone, on your Tailscale network, can open the page. Each enrolment is a security alert: "
                 + "you get a mail about it, and when the mailroom keeps your mail off Gmail, a notification on this "
                 + "Mac too.")
                .fixedSize(horizontal: false, vertical: true)
            if let e = enrolment {
                HStack(alignment: .top, spacing: 16) {
                    if let qr = PhoneQR.image(e.url, scale: 8) {
                        Image(nsImage: qr).interpolation(.none).resizable().frame(width: 220, height: 220)
                    } else {
                        Text("The QR code could not be made. Open this address on the phone instead:\n\(e.url)")
                            .textSelection(.enabled).frame(width: 220, alignment: .leading)
                            .foregroundColor(AgentState.blocked.color)
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Works once, until \(MailFmt.formatter("HH:mm", .current).string(from: Date(timeIntervalSince1970: e.lapses))).")
                        Text("Showing a new code ends this one.").foregroundStyle(.secondary)
                    }
                }
            }
            if let s = said { Text(s).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
            HStack {
                Button("Sign the phone out") { signOut(forget: false) }.disabled(busy)
                Button("Forget the phone…") { confirmForget = true }.disabled(busy)
                Spacer()
                Button("Close") { model.sheet = nil }.keyboardShortcut(.cancelAction)
                Button(enrolment == nil ? "Show an enrolment code" : "Show a new code") { enrol() }
                    .disabled(busy).keyboardShortcut(.defaultAction)
            }
        }
        .padding(18)
        .frame(width: 560)
        .alert("Forget the phone?", isPresented: $confirmForget) {
            Button("Cancel", role: .cancel) {}
            Button("Forget it", role: .destructive) { signOut(forget: true) }
        } message: {
            Text("It is signed out and its passkey is no longer taken; it must be enrolled again with a new code.")
        }
    }

    private func enrol() {
        busy = true
        Actions.fetch(["action": "mail_web_enrol"], key: nil, as: PhoneEnrolment.self) { got, why in
            busy = false
            enrolment = got
            said = why.map { "No code: " + $0 }
        }
    }

    private func signOut(forget: Bool) {
        busy = true
        Actions.runJSON(["action": "mail_web_sign_out", "forget": forget]) { obj, err in
            busy = false
            guard let obj = obj, (obj["ok"] as? Bool) == true else {
                said = "Not signed out: " + ((obj?["error"] as? String) ?? err ?? "no answer from the desktop")
                return
            }
            guard let n = obj["sessions"] as? Int else {
                said = "The desktop's answer does not say how many sessions ended; the phone may still be signed in."
                return
            }
            said = (n == 1 ? "1 session ended" : "\(n) sessions ended")
                + (forget ? "; the phone's passkey is forgotten." : ".")
        }
    }
}

/// --selftest-phone: the QR code reads back as the address it was made from, and the desktop's answer decodes.
enum PhoneTest {
    static func run() -> Int32 {
        var failures: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { failures.append(what) } }
        let url = "https://desk.tail1.ts.net:8466/enrol#AbC-123_xyz0AbC-123_xyz0"
        let read: String? = {
            guard let img = PhoneQR.image(url, scale: 8), let tiff = img.tiffRepresentation,
                  let ci = CIImage(data: tiff),
                  let detector = CIDetector(ofType: CIDetectorTypeQRCode, context: nil,
                                            options: [CIDetectorAccuracy: CIDetectorAccuracyHigh]) else { return nil }
            return (detector.features(in: ci).first as? CIQRCodeFeature)?.messageString
        }()
        check(read == url, "the QR code reads back as its address: \(read ?? "nothing read")")
        let (got, why) = Actions.decodeAnswer(["ok": true, "url": url, "lapses": 1600.0], nil, key: nil,
                                              as: PhoneEnrolment.self)
        check(got == PhoneEnrolment(url: url, lapses: 1600) && why == nil, "the enrolment answer decodes")
        let (none, refused) = Actions.decodeAnswer(["ok": false, "error": "not running"], nil, key: nil,
                                                   as: PhoneEnrolment.self)
        check(none == nil && refused == "not running", "a refusal says why")
        failures.forEach { print("FAIL: " + $0) }
        print(failures.isEmpty ? "phone selftest: ok" : "phone selftest: \(failures.count) failed")
        return failures.isEmpty ? 0 : 1
    }
}
