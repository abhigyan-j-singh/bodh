# bodh for Mac: the engine, the app, the bundle, the signature, the DMG. Run on a Mac with
# Xcode (or the Command Line Tools) and Python 3.10+ on the PATH. `make app` is the whole
# development build; `make dist` the signed, notarized DMG once IDENTITY and NOTARY_PROFILE
# are set (docs/release.md).
#
#   make engine          the bundled interpreter with bodh installed (scripts/build_engine.sh)
#   make build           swift build, release
#   make app             Bodh.app in build/, signed ad hoc: open build/Bodh.app
#   make run             the app from the build folder, the engine from build/engine-<arch>
#   make test            the engine library's tests (swift test)
#   make icon            AppIcon.icns from Resources/Icon (iconutil)
#   make sign            Developer ID signature (IDENTITY=...), every Mach-O in the engine too
#   make notarize        notarytool + staple (NOTARY_PROFILE=bodh)
#   make dmg             build/bodh-<version>-<arch>.dmg with its checksum
#   make dist            sign, notarize, dmg
#   make clean

ARCH ?= $(shell uname -m | sed 's/aarch64/arm64/')
BUNDLE_ID ?= in.antriksh.bodh
VERSION ?= $(shell cd .. && python3 -c "import re,pathlib; print(re.search(r'__version__ = \"([^\"]+)\"', pathlib.Path('src/bodh/__init__.py').read_text()).group(1))")
BUILD ?= $(shell git -C .. rev-list --count HEAD 2>/dev/null || echo 1)
ENGINE := build/engine-$(ARCH)
APP := build/Bodh.app
BIN_DIR = $(shell swift build -c release --arch $(ARCH) --show-bin-path 2>/dev/null || echo .build/release)
BINARY = $(BIN_DIR)/Bodh
IDENTITY ?=
NOTARY_PROFILE ?= bodh
ENTITLEMENTS ?= default

.PHONY: engine build app run test icon sign notarize dmg dist clean check

engine:
	../scripts/build_engine.sh --arch $(ARCH) --out $(ENGINE)

build:
	swift build -c release --arch $(ARCH)

app: build icon
	@test -x $(ENGINE)/python/bin/python3 || $(MAKE) engine
	scripts/make_app.sh --binary $(BINARY) --engine $(ENGINE) --out $(APP) --version $(VERSION) --build $(BUILD) --bundle-id $(BUNDLE_ID)
	scripts/sign.sh $(APP)
	@echo "open $(APP)"

run: build
	@test -x $(ENGINE)/python/bin/python3 || $(MAKE) engine
	BODH_ENGINE=$(abspath $(ENGINE)) $(BINARY)

test:
	swift test

icon: build/AppIcon.icns

build/AppIcon.icns: Resources/Icon/icon-1024.png
	@mkdir -p build/AppIcon.iconset
	@for s in 16 32 128 256 512; do \
	  sips -z $$s $$s Resources/Icon/icon-1024.png --out build/AppIcon.iconset/icon_$${s}x$${s}.png >/dev/null; \
	  d=$$((s*2)); sips -z $$d $$d Resources/Icon/icon-1024.png --out build/AppIcon.iconset/icon_$${s}x$${s}@2x.png >/dev/null; \
	done
	iconutil -c icns build/AppIcon.iconset -o build/AppIcon.icns
	@echo "wrote build/AppIcon.icns"

sign:
	@test -n "$(IDENTITY)" || (echo 'set IDENTITY="Developer ID Application: Name (TEAMID)" (security find-identity -v -p codesigning)' && exit 2)
	IDENTITY="$(IDENTITY)" scripts/sign.sh $(APP) $(ENTITLEMENTS)

notarize:
	NOTARY_PROFILE=$(NOTARY_PROFILE) scripts/notarize.sh $(APP)

dmg:
	scripts/make_dmg.sh $(APP) $(VERSION) $(ARCH) build
	NOTARY_PROFILE=$(NOTARY_PROFILE) scripts/notarize.sh build/bodh-$(VERSION)-$(ARCH).dmg

dist: app sign notarize dmg

check:
	@echo "version $(VERSION) build $(BUILD) arch $(ARCH) bundle $(BUNDLE_ID)"
	@xcodebuild -version 2>/dev/null || echo "xcodebuild: not found (Xcode or the Command Line Tools)"
	@security find-identity -v -p codesigning | grep -c 'Developer ID Application' | sed 's/^/Developer ID identities: /' || true
	@xcrun notarytool history --keychain-profile $(NOTARY_PROFILE) >/dev/null 2>&1 && echo "notarytool profile $(NOTARY_PROFILE): ok" || echo "notarytool profile $(NOTARY_PROFILE): not set up (docs/release.md)"

clean:
	rm -rf build .build
