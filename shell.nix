# `nix-shell` without a flake. `nix develop` is the supported shell.
{ pkgs ? import <nixpkgs> { } }:
let
  disktree = pkgs.callPackage ./nix/package.nix { };
in
pkgs.mkShell {
  inputsFrom = [ disktree ];
  packages = [
    pkgs.rustc
    pkgs.cargo
    pkgs.clippy
    pkgs.rustfmt
  ];
}
