{
  description = "disktree — a treemap of what is using a disk";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system: {
        default = nixpkgs.legacyPackages.${system}.callPackage ./nix/package.nix { };
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            inputsFrom = [ self.packages.${system}.default ];
            packages = [
              pkgs.rustc
              pkgs.cargo
              pkgs.clippy
              pkgs.rustfmt
            ];
          };
        }
      );

      checks = forAllSystems (system: {
        disktree = self.packages.${system}.default;
      });

      nixosModules.default = ./nix/module.nix;

      overlays.default = final: _prev: {
        disktree = self.packages.${final.stdenv.hostPlatform.system}.default;
      };
    };
}
