# nexus-ai-extensions

Browser extensions for [Nexus-AI](https://github.com/hy714335634/Nexus-AI).

## Current

- **`chrome/`** — Chrome MV3 extension (the "browser agent" surface — Nexus
  agents drive real browser tabs via this extension through the
  `browser_navigate` / `browser_act` / `browser_observe` /
  `wait_for_human` tools).

## Load the dev build

```
1. Open chrome://extensions
2. Enable Developer Mode
3. "Load unpacked" → select `chrome/`
```

See `chrome/README.md` (if present) or the parent project's
`docs/browser-extension-install.md` for paired-Nexus setup (token
issuance, WSS URL, etc.).

## Tests

```bash
cd chrome
npm install    # (node 18+, nothing beyond devDeps)
npm test       # runs node --test against ../tests/unit/extension/*.js
```

81 unit tests covering the badge state machine, session map, WSS URL
normalization (with the P1-1 regression guard), protocol version
semver, content-script helpers, and token-paste whitespace scrub.

## Repo layout

```
.
├── chrome/          # Chrome MV3 extension source + build scripts
│   ├── manifest.json
│   ├── background/
│   ├── content/
│   ├── options/
│   ├── icons/
│   ├── scripts/     # build.sh + gen_icons.py
│   └── package.json # npm test + build entry points
└── tests/
    └── unit/
        └── extension/  # 6 Node --test suites (81 tests total)
```

Future additions (Firefox, Safari) would sit as sibling directories
next to `chrome/`, sharing `tests/`.

## Consumed as a git submodule

Nexus-AI pulls this repo in as `extensions/` via git submodule. After
cloning Nexus-AI:

```bash
git submodule update --init --recursive
```

## License

MIT (same as Nexus-AI).
