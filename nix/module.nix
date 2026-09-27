# `programs.disktree` for NixOS and for home-manager. The two evaluations
# are separate: import `nixosModules` from a NixOS configuration and
# `homeModules` from a home-manager one. Importing both into one evaluation
# would define the option twice.
{ self }:
let
  program =
    install:
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
      options.programs.disktree = {
        enable = lib.mkEnableOption "disktree, a disk-usage treemap";
        package = lib.mkOption {
          type = lib.types.package;
          description = "The disktree package to install.";
          default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
          defaultText = lib.literalExpression "disktree.packages.\${system}.default";
        };
      };

      config = lib.mkIf cfg.enable (install cfg);
    };
in
{
  nixos = program (cfg: {
    environment.systemPackages = [ cfg.package ];
  });

  homeManager = program (cfg: {
    home.packages = [ cfg.package ];
  });
}
