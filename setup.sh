#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- Detect Python 3.12+ ---
PYTHON=""
for candidate in python3.13 python3.12 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
        major="${version%%.*}"
        minor="${version##*.}"
        if [[ "$major" == "3" && "$minor" -ge 12 ]]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "Python 3.12+ not found. Attempting to install..."
    OS="$(uname -s)"

    if [[ "$OS" == "Linux" ]]; then
        if ! command -v apt-get &>/dev/null; then
            echo "Error: apt-get not found. Please install Python 3.12+ manually: https://www.python.org/downloads/"
            exit 1
        fi
        # Ubuntu < 24.04 doesn't ship Python 3.12 in default repos; add deadsnakes PPA if needed
        if ! apt-cache show python3.12 &>/dev/null 2>&1; then
            echo "Adding deadsnakes PPA for Python 3.12..."
            sudo apt-get install -y software-properties-common
            sudo add-apt-repository -y ppa:deadsnakes/ppa
            sudo apt-get update -y
        fi
        sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
        PYTHON="python3.12"

    elif [[ "$OS" == "Darwin" ]]; then
        if ! command -v brew &>/dev/null; then
            echo "Homebrew not found. Installing Homebrew (requires sudo)..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            # Add brew to PATH for this session (Apple Silicon path; falls back to Intel)
            eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)" || true
        fi
        echo "Installing Python 3.12 via Homebrew..."
        brew install python@3.12
        BREW_PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
        [[ -x "$BREW_PYTHON" ]] && PYTHON="$BREW_PYTHON" || PYTHON="python3.12"

    else
        echo "Error: Unsupported OS '$OS'. Please install Python 3.12+ manually: https://www.python.org/downloads/"
        exit 1
    fi

    if ! command -v "$PYTHON" &>/dev/null; then
        echo "Error: Python installation failed. Please install Python 3.12+ manually: https://www.python.org/downloads/"
        exit 1
    fi
fi

echo "Using $PYTHON ($("$PYTHON" --version))"

# --- Create venv if needed ---
if [[ ! -d ".venv" ]]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv .venv
fi

# --- Activate venv ---
source .venv/bin/activate

# --- Install project ---
echo "Installing agent-queue and dependencies..."
pip install -e ".[dev]" --quiet

# --- Shell completion for `aq` CLI ---
SHELL_NAME="$(basename "$SHELL")"
case "$SHELL_NAME" in
    bash)
        COMP_FILE="$HOME/.aq-complete.bash"
        RC_FILE="$HOME/.bashrc"
        _AQ_COMPLETE=bash_source aq > "$COMP_FILE" 2>/dev/null
        SOURCE_LINE="source $COMP_FILE"
        ;;
    zsh)
        COMP_FILE="$HOME/.aq-complete.zsh"
        RC_FILE="$HOME/.zshrc"
        _AQ_COMPLETE=zsh_source aq > "$COMP_FILE" 2>/dev/null
        SOURCE_LINE="source $COMP_FILE"
        ;;
    fish)
        COMP_FILE="$HOME/.config/fish/completions/aq.fish"
        RC_FILE=""
        mkdir -p "$(dirname "$COMP_FILE")"
        _AQ_COMPLETE=fish_source aq > "$COMP_FILE" 2>/dev/null
        SOURCE_LINE=""
        ;;
    *)
        COMP_FILE=""
        RC_FILE=""
        SOURCE_LINE=""
        ;;
esac

if [[ -n "$COMP_FILE" && -s "$COMP_FILE" ]]; then
    if [[ -n "$SOURCE_LINE" && -n "$RC_FILE" ]]; then
        if ! grep -qF "$SOURCE_LINE" "$RC_FILE" 2>/dev/null; then
            echo ""
            echo "Shell completion for 'aq' is available."
            read -rp "Add tab-completion to $RC_FILE? [Y/n] " answer
            if [[ "${answer:-Y}" =~ ^[Yy]$ ]]; then
                echo "$SOURCE_LINE" >> "$RC_FILE"
                echo "Added to $RC_FILE. Restart your shell or run: $SOURCE_LINE"
            else
                echo "Skipped. To enable later, add this to $RC_FILE:"
                echo "  $SOURCE_LINE"
            fi
        fi
    elif [[ "$SHELL_NAME" == "fish" ]]; then
        echo "Fish shell completion for 'aq' installed to $COMP_FILE"
    fi
fi

# --- Run setup wizard ---
echo ""
.venv/bin/python src/setup_wizard.py
