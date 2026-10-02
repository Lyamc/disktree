# NixOS module. Import it from configuration.nix and set
# `programs.disktree.enable`. The package is this tree, built by
# `nix/package.nix`, unless `programs.disktree.package` says otherwise.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.disktree;
in
{
  imports = [ ./web.nix ];

  options.programs.disktree = {
    enable = lib.mkEnableOption "disktree, a disk-usage treemap";
    package = lib.mkOption {
      type = lib.types.package;
      description = "The disktree package to install.";
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];
  };
}
