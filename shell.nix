{ pkgs ? import <nixpkgs> {} }:

let
  python = pkgs.python312;

  pythonWithTk = python.withPackages (ps: [
    ps.tkinter
    ps.pip
  ]);

  tplinkVersion = "5.36.0";
in
pkgs.mkShell {
  packages = [
    pythonWithTk
    pkgs.cacert
  ];

  shellHook = ''
    export VENV_DIR="$PWD/.venv-deco"

    # Include Nix-provided tkinter and its compiled extension in the venv.
    export PYTHONPATH="${pythonWithTk}/${python.sitePackages}''${PYTHONPATH:+:$PYTHONPATH}"

    export SSL_CERT_FILE="${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
    export PIP_REQUIRE_VIRTUALENV=true
    export PIP_DISABLE_PIP_VERSION_CHECK=1

    deco_setup() {
      local expected_python="${pythonWithTk}"
      local marker="$VENV_DIR/.nix-python-environment"

      if [ -e "$VENV_DIR" ]; then
        if [ ! -f "$marker" ] ||
           [ "$(cat "$marker")" != "$expected_python" ]; then
          echo "Existing $VENV_DIR was created with a different environment."
          echo "Move it aside, then re-enter nix-shell:"
          echo "  mv .venv-deco .venv-deco.backup"
          return 1
        fi
      else
        echo "Creating $VENV_DIR..."
        "${pythonWithTk}/bin/python" -m venv "$VENV_DIR" || return 1
        printf '%s\n' "$expected_python" > "$marker"
      fi

      source "$VENV_DIR/bin/activate" || return 1

      # Install only if the requested version is not already present.
      if ! python -c \
        'from importlib.metadata import version; assert version("tplinkrouterc6u") == "${tplinkVersion}"' \
        >/dev/null 2>&1; then
        python -m pip install \
          "tplinkrouterc6u==${tplinkVersion}" || return 1
      fi

      python -c \
        'import tkinter; import tplinkrouterc6u; print("Tkinter import OK; Tk version:", tkinter.TkVersion)' \
        || return 1

      echo
      echo "Deco environment ready."
      echo "  python deco_backend.py"
      echo "  python deco_gui.py"
      echo
      echo "Tkinter window test:"
      echo "  python -m tkinter"
    }

    deco_setup
    unset -f deco_setup
  '';
}
