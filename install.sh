#!/usr/bin/env bash
# Bootstrap a detached source snapshot; application/runtime work stays unprivileged.
set -euo pipefail

fail() { printf 'CamPiX bootstrap: %s\n' "$*" >&2; exit 1; }

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    fail 'Run as a regular user. Only package-manager and native service steps use sudo.'
fi

ref="${CAMPIX_REF:-main}"
[[ "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "$ref" != *..* ]] || fail 'Invalid CAMPIX_REF; use a commit, tag, or branch name.'

# An inspected local checkout can report its plan without downloads or elevation.
for arg in "$@"; do
    if [[ "$arg" == --dry-run ]]; then
        local_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-.}")" && pwd)"
        if [[ -f "$local_root/scripts/install.py" ]] && command -v python3 >/dev/null; then
            exec python3 "$local_root/scripts/install.py" "$@"
        fi
        printf 'CamPiX bootstrap dry run: OS=%s architecture=%s source-ref=%s\n' "$(uname -s)" "$(uname -m)" "$ref"
        printf '%s\n' 'A normal run installs missing git/Python bootstrap prerequisites, fetches a detached source snapshot, and runs its platform installer.'
        exit 0
    fi
done

elevate() {
    command -v sudo >/dev/null || fail 'sudo is required to install missing system prerequisites.'
    sudo "$@"
}

python_ready=false
if command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    python_ready=true
fi
if ! command -v git >/dev/null || [[ "$python_ready" != true ]]; then
    case "$(uname -s)" in
        Linux)
            if command -v apt-get >/dev/null; then
                elevate apt-get update
                elevate apt-get install -y --no-install-recommends git python3 ca-certificates
            elif command -v dnf >/dev/null; then
                elevate dnf install -y git python3 ca-certificates
            elif command -v pacman >/dev/null; then
                # Do not run a partial Arch Linux distribution upgrade.
                elevate pacman -S --needed --noconfirm git python ca-certificates
            elif command -v zypper >/dev/null; then
                elevate zypper --non-interactive install git python3 ca-certificates
            else
                fail 'No supported bootstrap package manager (apt, dnf, pacman, zypper).'
            fi
            ;;
        Darwin)
            command -v brew >/dev/null || fail 'Install Homebrew from https://brew.sh, then rerun this command.'
            brew install git python@3.12
            export PATH="$(brew --prefix python@3.12)/libexec/bin:$PATH"
            ;;
        *) fail 'Unsupported bootstrap platform; use install.ps1 on Windows.' ;;
    esac
fi

command -v git >/dev/null || fail 'git is still unavailable after prerequisite installation.'
command -v python3 >/dev/null || fail 'python3 is still unavailable after prerequisite installation.'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || fail 'Python 3.11+ is required. Upgrade the OS for native CSI; do not replace its camera Python environment.'

stage="$(mktemp -d "${TMPDIR:-/tmp}/campix-bootstrap.XXXXXXXX")"
export GIT_TERMINAL_PROMPT=0
export GIT_HTTP_LOW_SPEED_LIMIT=1000
export GIT_HTTP_LOW_SPEED_TIME=60
printf 'CamPiX source staging: %s\n' "$stage"
git init --quiet "$stage/source"
git -C "$stage/source" remote add origin https://github.com/DensenDeluxe/CamPiX.git
git -C "$stage/source" -c credential.interactive=false fetch --quiet --depth 1 origin "$ref"
git -C "$stage/source" checkout --quiet --detach FETCH_HEAD
commit="$(git -C "$stage/source" rev-parse HEAD)"
printf 'CamPiX source commit: %s\n' "$commit"
[[ -f "$stage/source/scripts/install.py" ]] || fail 'This source revision has no all-in-one installer. Select a published installer revision with CAMPIX_REF.'

# A pipe-based one-liner still needs a real terminal for the private account prompt.
if [[ ! -t 0 ]]; then
    for arg in "$@"; do
        if [[ "$arg" == --non-interactive ]]; then
            exec python3 "$stage/source/scripts/install.py" "$@"
        fi
    done
    [[ -r /dev/tty ]] || fail 'No terminal available. Run from an interactive terminal for administrator setup.'
    exec python3 "$stage/source/scripts/install.py" "$@" </dev/tty
fi
exec python3 "$stage/source/scripts/install.py" "$@"
