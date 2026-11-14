<p align="center">
  <img src="assets/svg/arrsenal-of-scripts-logo.svg" alt="arrsenal-of-scripts logo" width="160" height="160">
</p>

<h1 align="center">arrsenal-of-scripts</h1>

<p align="center"><strong>Scripts for server management, Servarr automation, and developer tools.</strong></p>

<p align="center">
  <a href="https://github.com/edbfi/arrsenal-of-scripts/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/License-AGPL--3.0--or--later-blue?style=flat" alt="License: AGPL-3.0-or-later">
  </a>
  <a href="https://github.com/python/cpython" title="CPython source repository">
    <img src="https://img.shields.io/badge/Python-3776AB?style=flat&amp;logo=python&amp;logoColor=white" alt="Python">
  </a>
  <a href="https://git.savannah.gnu.org/cgit/bash.git/" title="GNU Bash source repository">
    <img src="https://img.shields.io/badge/Bash-4EAA25?style=flat&amp;logo=gnubash&amp;logoColor=white" alt="Bash">
  </a>
  <a href="https://sourceforge.net/p/zsh/code/" title="Zsh source repository">
    <img src="https://img.shields.io/badge/Zsh-F15A24?style=flat&amp;logo=zsh&amp;logoColor=white" alt="Zsh">
  </a>
  <a href="https://github.com/edbfi/arrsenal-of-scripts/actions/workflows/ci.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/edbfi/arrsenal-of-scripts/ci.yml?branch=main&amp;event=push&amp;label=CI&amp;style=flat" alt="CI"></a>
</p>

## 🚀 About the Repo

This is a collection of scripts I’ve thrown together for managing servers, automating tasks, and tinkering with apps like **Sonarr**, **Radarr**, **Prowlarr**, and more. It’s mostly for personal use, but feel free to adapt it to your needs.

## 📂 What You'll Find

- **'Arr' App Scripts**: Odd scripts for the "Servarr" app suite
- **Server Management Tools**: Odd scripts for my personal server management, including [temporary sudo access for AI coding agents](server-scripts/sudo/ai-agent-sudo.zsh).
- **Dev Tools**: [upkeep](dev-tools/upkeep/README.md) keeps AI and developer CLIs (claude, codex, agy, uv, ruff, prek, bun, Homebrew, ACPs, …) installed and updated across hosts, and the [T3 Code Antigravity ACP updater](dev-tools/t3-antigravity-acp/README.md) it drives.
- **Miscellaneous Scripts**: Random scripts I found useful or fun to create.
- **Other Tools**: Whatever else made sense to throw in here.


## 🔧 How to Use

1. Clone the repository:
   ```bash
   git clone https://github.com/edbfi/arrsenal-of-scripts.git
   ```
2. Explore the scripts in the folders and pick what you need.
3. Review each script's configuration and adapt it to your setup before running it.

Review changes manually. Hostnames, usernames, paths, and service defaults are preserved examples and may not match your system.
The legacy Hotio migration helper retains its original `hotio/` and `engels74/`
image matching; it is not an edbfi image-namespace migration tool.

## ⚠️ Disclaimer

These scripts were written for personal use and might not suit every setup. Use at your own risk and feel free to modify them as needed.

## ⚖️ License

Unless otherwise noted, the original code in this repository is free software: you
may redistribute it and/or modify it under the terms of the GNU Affero General
Public License as published by the Free Software Foundation, either version 3 of
the License, or (at your option) any later version (`AGPL-3.0-or-later`). See
[LICENSE](LICENSE) for the full license text.

Third-party and vendored material retains its existing licenses and notices; this
grant does not replace those terms or revoke earlier grants.
