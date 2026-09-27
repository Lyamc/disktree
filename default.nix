# `nix-build` without a flake. The flake is the supported entry point.
{ pkgs ? import <nixpkgs> { } }:
pkgs.callPackage ./nix/package.nix { }
