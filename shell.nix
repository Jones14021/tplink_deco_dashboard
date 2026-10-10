{ pkgs ? import <nixpkgs> {} }:

let
  python = pkgs.python312;

  pythonWithTk = python.withPackages (ps: [
    ps.tkinter
    ps.pip
  ]);
in
pkgs.mkShell {
  packages = [
    pythonWithTk
    pkgs.cacert
  ];

  shellHook = ''
    export VENV_DIR="$PWD/.venv-deco"

    # Provide matching Nix-built Tkinter to the venv.
    export PYTHONPATH="${pythonWithTk}/${python.sitePackages}"
    unset PYTHONHOME

    export SSL_CERT_FILE="${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
    export PIP_REQUIRE_VIRTUALENV=true
    export PIP_DISABLE_PIP_VERSION_CHECK=1

    deco_setup() {
      local expected_python="${pythonWithTk}"
      local marker="$VENV_DIR/.nix-python-environment"

      if [ -e "$VENV_DIR" ]; then
        if [ ! -f "$marker" ] ||
           [ "$(cat "$marker")" != "$expected_python" ]; then
          echo "ERROR: Existing venv belongs to another Nix environment."
          echo "Move it aside and re-enter nix-shell:"
          echo "  mv .venv-deco .venv-deco.backup"
          return 1
        fi
      else
        echo "[1/4] Creating venv..."
        "${pythonWithTk}/bin/python" -m venv "$VENV_DIR" || return 1
        printf '%s\n' "$expected_python" > "$marker"
      fi

      echo "[2/4] Activating venv..."

      # If nix-shell inherited an activated venv, clean that up first.
      # This also avoids stacking prompt prefixes.
      if declare -F deactivate >/dev/null 2>&1; then
        deactivate || true
      fi

      unset VIRTUAL_ENV_DISABLE_PROMPT

      # Activation may return nonzero despite applying its changes.
      # Validate the resulting environment instead of trusting that status.
      source "$VENV_DIR/bin/activate" || true
      hash -r 2>/dev/null || true

      if [ "$VIRTUAL_ENV" != "$VENV_DIR" ] ||
        [ "$(command -v python)" != "$VENV_DIR/bin/python" ]; then
        echo "ERROR: Venv activation did not select the expected interpreter."
        return 1
      fi

      # Restore the Nix package path after activation/deactivation.
      export PYTHONPATH="${pythonWithTk}/${python.sitePackages}"
      unset PYTHONHOME

      echo "[3/4] Installing TP-Link library..."
      "$VIRTUAL_ENV/bin/python" -m pip install \
        "tplinkrouterc6u==5.36.0" || return 1

      echo "[4/4] Checking imports..."
      "$VIRTUAL_ENV/bin/python" -c \
        'import sys; import tkinter; import tplinkrouterc6u; print("Python:", sys.executable); print("Tk version:", tkinter.TkVersion); print("TP-Link library:", tplinkrouterc6u.__file__)' \
        || return 1

      echo
      echo "Environment ready:"
      echo "  python -m tkinter"
      echo "  python deco_gui.py"
      echo "  python deco_backend.py"
    }

    if deco_setup; then
      :
    else
      echo
      echo "ERROR: Deco environment setup failed."
    fi

    unset -f deco_setup
  '';
}
