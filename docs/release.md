# Releasing the Mac app

What only the owner's Apple account can do, done once, and what every release then runs.
The scripts are in `mac/scripts/`; `mac/Makefile` strings them together; the GitHub workflow
`.github/workflows/mac.yml` runs the same on a tag once the secrets below exist.

## Once

1. **Xcode and the tools.** Xcode 15 or later from the App Store (or the Command Line Tools,
   `xcode-select --install`, which is enough for `swift build`), then `sudo xcodebuild -license
   accept`. `make -C mac check` prints what is found.
2. **The Developer ID certificate.** In Xcode › Settings › Accounts, sign in with the Apple ID
   of the Developer Program membership, Manage Certificates, add a *Developer ID Application*
   certificate. `security find-identity -v -p codesigning` lists it as
   `Developer ID Application: Your Name (TEAMID)`; that string is `IDENTITY` below.
3. **The notarytool profile.** An app-specific password from appleid.apple.com (Sign-In and
   Security › App-Specific Passwords), then
   `xcrun notarytool store-credentials bodh --apple-id YOU@EXAMPLE.COM --team-id TEAMID --password THE-APP-SPECIFIC-PASSWORD`.
   The profile name `bodh` is `NOTARY_PROFILE`'s default. `xcrun notarytool history
   --keychain-profile bodh` answers once it works.
4. **The Sparkle keys** (when Sparkle ships): `generate_keys` from the Sparkle distribution
   once; the public key goes into `Resources/Info.plist` as `SUPublicEDKey`, the private key
   stays in the Keychain of the Mac that releases (and in the GitHub secret `SPARKLE_PRIVATE_KEY`
   for the workflow), never in the repository.
5. **The bundle id.** `in.antriksh.bodh` is the Makefile's default (`BUNDLE_ID`); it never
   changes once a version has shipped, so settle it before the first signed build.
6. **The GitHub secrets** for the workflow: `MAC_CERTIFICATE_P12` (the Developer ID certificate
   exported from Keychain Access as a .p12, base64-encoded), `MAC_CERTIFICATE_PASSWORD`,
   `NOTARY_APPLE_ID`, `NOTARY_TEAM_ID`, `NOTARY_PASSWORD` (the app-specific password),
   and later `SPARKLE_PRIVATE_KEY`.

## Every release

```
cd mac
make app                                   # the engine, the build, Bodh.app signed ad hoc
make sign IDENTITY="Developer ID Application: Your Name (TEAMID)"
make notarize                              # NOTARY_PROFILE=bodh; a few minutes
make dmg                                   # build/bodh-<version>-<arch>.dmg, notarized, stapled, with its .sha256
```

`make dist` runs the four in order. The version comes from `src/bodh/__init__.py` (the app's
version equals the package's; one tag releases both), the build number from the commit count.
Build the Intel DMG on an Intel Mac, or on Apple silicon with Rosetta installed (`make
ARCH=x86_64 dist`: the x86_64 engine runs under Rosetta during the build).

Then: a GitHub release from the tag with both DMGs and their checksums; the Homebrew cask in
`abhigyan-j-singh/homebrew-tap` updated (`docs/mac.md` has the cask); and, with Sparkle, the
appcast regenerated (`generate_appcast build/`) and uploaded as `appcast.xml`.

## Checks before the release

- `codesign --verify --deep --strict --verbose=2 build/Bodh.app` and `spctl --assess --type
  execute build/Bodh.app` both pass (the scripts run them).
- On a clean user account (System Settings › Users & Groups, a new user): download the DMG,
  drag the app to Applications, open it: no Gatekeeper warning; onboarding to the first run
  without a terminal.
- `ENTITLEMENTS=strict make sign` then the Keychain read (Settings › Account › Check with a
  stored key): if it works, drop `allow-unsigned-executable-memory` from
  `Resources/engine.entitlements`.
- Activity Monitor: the app's Energy Impact low over an hour idle; the engine under 100 MB.

## Rotation

A Developer ID certificate lasts five years; a new one is made in Xcode the same way and the
`.p12` secret replaced. The app-specific password is revoked and remade at appleid.apple.com
(`store-credentials` again). The Sparkle private key never rotates on its own: a new key means
the old app cannot verify the new appcast, so a release signed with both keys bridges it.
