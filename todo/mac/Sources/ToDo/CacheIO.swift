import Foundation

/// A cache file that can't be read, or couldn't be saved. `message` is written to be shown in the window as it is.
struct CacheError: Error {
    var message: String
}

/// The app's own files under ~/Library/Application Support/ToDo (the task cache with its unsent edits). A missing
/// file means an empty cache. A file that is there but can't be read is never
/// silently replaced: it is renamed aside to `<name>.bad-<time>`, and the caller shows the error and starts fresh.
enum CacheIO {
    /// The decoded file; nil when there is no file. An unreadable one is moved aside and reported as the error.
    static func load<T>(_ url: URL, _ decode: (Data) throws -> T) -> Result<T?, CacheError> {
        guard FileManager.default.fileExists(atPath: url.path) else { return .success(nil) }
        do {
            return .success(try decode(try Data(contentsOf: url)))
        } catch {
            let stamp = isoString(Date()).replacingOccurrences(of: ":", with: "")
            let aside = url.deletingLastPathComponent().appendingPathComponent(url.lastPathComponent + ".bad-" + stamp)
            do {
                try FileManager.default.moveItem(at: url, to: aside)
            } catch let moveError {
                return .failure(CacheError(message: "\(url.lastPathComponent) couldn't be read (\(error.localizedDescription)) "
                                           + "or moved aside (\(moveError.localizedDescription)); starting with an empty cache"))
            }
            return .failure(CacheError(message: "\(url.lastPathComponent) couldn't be read (\(error.localizedDescription)); "
                                       + "it was kept as \(aside.lastPathComponent) and the app started with an empty cache"))
        }
    }

    /// Encode and write atomically, making the folder if need be.
    static func save(_ url: URL, _ encode: () throws -> Data) throws {
        do {
            let data = try encode()
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: url, options: .atomic)
        } catch {
            throw CacheError(message: "Couldn't save \(url.lastPathComponent): \(error.localizedDescription)")
        }
    }
}
