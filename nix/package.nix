# The installable disktree: the release binary, the launcher entry and the
# icon. The same three files `make install` puts under a prefix, laid out
# the way NixOS and home-manager expect.
#
# GPUI loads the Vulkan loader, Wayland and fontconfig by name when the
# window opens, so those libraries have to be on the binary's run path.
# NixOS keeps the GPU driver beside the loader in /run/opengl-driver; the
# loader package here is what finds that driver.
{
  lib,
  rustPlatform,
  pkg-config,
  makeWrapper,
  patchelf,
  desktop-file-utils,
  wayland,
  libxkbcommon,
  libglvnd,
  vulkan-loader,
  vulkan-headers,
  fontconfig,
  freetype,
  libx11,
  libxcursor,
  libxi,
  libxcb,
  git,
  xdg-utils,
}:

let
  cargoToml = builtins.fromTOML (builtins.readFile ../Cargo.toml);
  version = cargoToml.workspace.package.version;

  # dlopen'd at runtime. Linked libraries already get a run path from the
  # cc wrapper; these are the ones a name lookup still has to find.
  runtimeLibs = [
    vulkan-loader
    libglvnd
    wayland
    libxkbcommon
    fontconfig
    freetype
    libx11
    libxcursor
    libxi
    libxcb
  ];
in

rustPlatform.buildRustPackage (finalAttrs: {
  pname = "disktree";
  inherit version;

  src = lib.cleanSourceWith {
    src = ../.;
    filter =
      path: type:
      let
        base = baseNameOf path;
      in
      # A local `cargo build` leaves target/ next to the sources. It is not
      # part of the package, and copying it into the store is enormous.
      (type != "directory" || (base != "target" && base != "result"))
      && lib.cleanSourceFilter path type;
  };

  cargoLock.lockFile = ../Cargo.lock;

  cargoBuildFlags = [
    "--package"
    "disktree-app"
  ];

  # The window-harness tests open a real window. The core crate's tests
  # (sizes, layout, removal guards) do not, and they run against the same
  # release profile as the binary so the app is not compiled twice.
  cargoTestFlags = [
    "--package"
    "disktree-core"
  ];
  cargoCheckType = "release";

  nativeBuildInputs = [
    pkg-config
    rustPlatform.bindgenHook
    makeWrapper
  ];

  buildInputs = [
    wayland
    libxkbcommon
    libglvnd
    vulkan-loader
    vulkan-headers
    fontconfig
    freetype
    libx11
    libxcursor
    libxi
    libxcb
  ];

  strictDeps = true;

  postInstall = ''
    install -Dm644 assets/disktree.svg \
      "$out/share/icons/hicolor/scalable/apps/disktree.svg"
    install -Dm644 packaging/disktree.desktop.in \
      "$out/share/applications/disktree.desktop"
    substituteInPlace "$out/share/applications/disktree.desktop" \
      --replace-fail @BINDIR@ "$out/bin" \
      --replace-fail @VERSION@ ${finalAttrs.version}
    # desktop-file-validate rejects a carriage return. A Windows checkout
    # of this tree has them; the file in git does not.
    sed -i 's/\r$//' "$out/share/applications/disktree.desktop"
  '';

  postFixup = ''
    ${lib.getExe patchelf} --add-rpath ${lib.makeLibraryPath runtimeLibs} \
      "$out/bin/disktree"
    # git is how a selected checkout reports its status. xdg-open is how
    # GPUI hands a path to the file manager. A user install of either wins,
    # because this is a suffix.
    wrapProgram "$out/bin/disktree" \
      --suffix PATH : ${
        lib.makeBinPath [
          git
          xdg-utils
        ]
      }
  '';

  doInstallCheck = true;
  nativeInstallCheckInputs = [ desktop-file-utils ];
  installCheckPhase = ''
    runHook preInstallCheck
    "$out/bin/disktree" --help | grep -q treemap
    desktop-file-validate "$out/share/applications/disktree.desktop"
    test -s "$out/share/icons/hicolor/scalable/apps/disktree.svg"
    runHook postInstallCheck
  '';

  meta = {
    description = "Treemap of what is using a disk, with marking and removal";
    homepage = "https://github.com/Lyamc/disktree";
    license = lib.licenses.mit;
    mainProgram = "disktree";
    platforms = lib.platforms.linux;
    # The binary is a Wayland/X11 window. --help works on a tty; opening
    # the treemap needs a session whose GPU driver Vulkan can see.
  };
})
