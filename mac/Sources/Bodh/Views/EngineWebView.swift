import AppKit
import SwiftUI
import WebKit
import BodhEngine

/// One of the engine's own pages (`/?mode=app&section=…`), loaded with the token in the
/// request; the page's live layer keeps it moving on its own. Links to articles and to BazQux
/// open in the default browser; the page itself stays in the window. Light and dark follow
/// the system through the page's own `prefers-color-scheme`.
struct EngineWebView: NSViewRepresentable {
    let client: EngineClient
    let section: String
    let generation: Int  // the port: a restarted engine means a fresh load

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = false
        view.setValue(false, forKey: "drawsBackground")
        if let identifier = Bundle.main.bundleIdentifier {
            view.customUserAgent = "bodh-mac/\(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev") (\(identifier))"
        }
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        let request = client.pageRequest(section: section)
        let key = "\(generation):\(request.url?.absoluteString ?? "")"
        guard context.coordinator.loaded != key else { return }
        context.coordinator.loaded = key
        view.load(request)
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var loaded: String?

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = navigationAction.request.url else { return decisionHandler(.allow) }
            let local = url.host == "127.0.0.1" || url.host == "localhost"
            if navigationAction.navigationType == .linkActivated && !local {
                NSWorkspace.shared.open(url)
                return decisionHandler(.cancel)
            }
            if navigationAction.navigationType == .linkActivated && local && url.fragment == nil {
                // the page's own links (the sections) stay in the window; anything else is a reload
                return decisionHandler(.allow)
            }
            decisionHandler(.allow)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            AppLog.warning("page failed: \(error.localizedDescription)")
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            AppLog.warning("page did not load: \(error.localizedDescription)")
            loaded = nil  // the next update loads it again (the engine was restarting, say)
        }
    }
}
