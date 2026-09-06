# PingLucas for Apple Watch

A native watchOS app and its iPhone companion. It subscribes to the same ntfy
topic the relay publishes on, lists what is waiting, and sends your answer back
with the routing tag attached.

It builds, for free, forever. **It probably cannot reach your wrist.** That
sentence needs the whole of the next section, because the reason is not the
$99.

## Read this before you invest a weekend in it

Three independent walls sit between a green CI build and an app on a Series 11,
and each one is sufficient on its own.

**1. Nothing outside Darwin can compile watchOS.** Swift's official cross-SDKs
are Static Linux, WebAssembly and Android; `swift-sdk-generator` states plainly
that macOS is supported only as a *host*. [xtool](https://github.com/xtool-org/xtool)
— the tool that made no-Mac iOS development real — extracts exactly three
platforms from `Xcode.xip`: `iPhoneOS`, `iPhoneSimulator`, `MacOSX`. There is
no `WatchOS.platform`, no `arm64_32-apple-watchos` triple, and no open issue
asking for one. That is why the build below runs on a macOS runner: not for
convenience, but because it is the only machine in existence that can do it.

**2. Sideloaders strip or reject the watch bundle.** AltStore never enumerates
`Watch/` at all, so the install fails outright with `Invalid value of
WKCompanionAppBundleIdentifier key` — its issue for this has been open since
May 2020. SideStore shares that code lineage. Sideloadly is worse: it vendors a
resigner with `REMOVE_WATCHKIT = True` and *silently deletes* the watch app,
with no flag and no warning. Only [Feather](https://github.com/khcrysalis/Feather)
rewrites the whole `WKCompanionAppBundleIdentifier` / `WKAppBundleIdentifier`
triangle correctly — and its maintainer's own note is that watch apps work
"if you have a proper certificate", meaning a paid one.

There is also a second, structural blocker: AltStore's device model is
`{iPhone, iPad, AppleTV}`. It can never register your Watch's UDID, and the
watch's own `installd` validates the profile's device list.

**3. No push, ever, on a free account.** Apple's capability tables list Push
Notifications as unavailable to a free Personal Team on both iOS and watchOS. A
free-account app cannot hold `aps-environment`, so it cannot receive APNs at
all — which is why this app polls rather than subscribes, and why it can only
notice a question while it is running or during a background refresh.

Worth knowing, since the internet gets it wrong: App Groups, Background Modes,
HealthKit and Keychain Sharing **are** available to a free team in 2026. Push
is the one that is not, and it is the one that matters here.

### What the free tier does give you

Ten App IDs per 7 days, expiring after 7 days; three devices; three apps per
device. A watch app burns **two** App IDs, so you get roughly five builds a
week before you are locked out, and then you reinstall every seven days
forever.

### So when is this app worth building?

- **You have Mac access**, even briefly. Xcode with a free Apple ID installs
  through your own iPhone. Renting one is cheap — [Scaleway Apple silicon is
  about €0.11/hour](https://www.scaleway.com/en/pricing/apple-silicon/), so a
  build session costs a euro or two.
- **You pay the $99.** Then the weekly treadmill stops, TestFlight works, and
  this becomes an ordinary app.
- **You want to keep the code honest.** The CI below proves every commit still
  compiles against the current watchOS SDK, which is worth having whether or
  not you ever install it.

**Until one of those is true, use Telegram.** As of June 2026 it has a native
watchOS 26 app with a real keyboard, it declares a text-input notification
action so you can answer from the raised wrist, and the relay already speaks
it. It is not a workaround — it is a better wrist experience than this app can
offer without push.

## Building it

Free and unlimited on a public repository — GitHub's standard macOS runners
carry no charge there. `.github/workflows/build-watchos.yml` selects Xcode
26.6, generates the project with XcodeGen, builds the watch scheme for both the
simulator and device slices, archives the iOS app unsigned, **asserts the watch
app was actually embedded**, and uploads an unsigned `.ipa`.

Three traps it already handles:

- Xcode 26 moved the embedded watch app from `Watch/` to `PlugIns/`
  (`dstSubfolderSpec` 16 → 13). XcodeGen still emits the old value, and Apple
  documented the change nowhere. The workflow patches the project file.
- Building the iOS scheme does **not** reliably build the embedded watch app —
  since Xcode 14 it can be silently omitted from the archive. The watch scheme
  is therefore built explicitly, and the archive is checked afterwards.
- `-downloadPlatform watchOS` is a multi-gigabyte tax if run unconditionally,
  and a two-second no-op when the runtime is already present. It is guarded.

Never switch to a `-large` or `-xlarge` runner: those are billed even on public
repositories.

Locally, on a Mac:

```bash
brew install xcodegen
cd apps/PingLucas && xcodegen generate && open PingLucas.xcodeproj
```

## Design

The colours are Anthropic's — the bone page `#F0EEE6`, the clay accent
`#D97757`, the warm near-black ink `#141413`, the `#1F1E1D`/`#262624` night
surfaces. Those carry the look faithfully.

The typefaces are not, and cannot be. Claude sets its interface in Styrene A/B
and its prose in Tiempos Text, both licensed from Klim Type Foundry and neither
redistributable in an open-source app. Shipping them would be a licence
violation dressed as a feature. The app uses the platform faces instead, which
on a 42mm display is the better call regardless: SF Compact is drawn for
exactly that size, and a borrowed grotesque would trade real legibility for a
little fidelity. If you license Styrene, drop the `.otf` files into
`Resources/Fonts` and set `Theme.brandFont`.

The app also does not use Anthropic's starburst mark. That is their trademark
and it does not belong on a third-party app icon; `PingMark` is a plain ring
with a clay dot for an unanswered question.

## Layout

```text
apps/PingLucas/
  project.yml                 XcodeGen spec; the .xcodeproj is generated, never committed
  Shared/
    Ping.swift                The model, and parsing the reply tag out of an ntfy title
    RelayClient.swift         ntfy polling and reply publishing
    PingStore.swift           Observable state, local answered-tracking
    Theme.swift               Anthropic's palette, shape and spacing
  Watch/Sources/              The wrist: pending list, answer view, quick replies
  iOS/Sources/                The phone: setup, inbox, settings
```
