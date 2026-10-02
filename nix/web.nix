# Browser treemap. Which directories are measured is configuration, not
# something baked into the program. The default is the filesystem root.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.disktree-web;
  configFile = pkgs.writeText "disktree-web.json" (
    builtins.toJSON {
      paths = cfg.paths;
      exclude = cfg.exclude;
      pacedPaths = cfg.pacedPaths;
    }
  );
  script = pkgs.writeText "disktree-web.py" (builtins.readFile ../web/disktree-web.py);
  absolute = lib.types.addCheck lib.types.str (p: lib.hasPrefix "/" p) // {
    name = "absolute-path";
    description = "absolute path";
  };
in
{
  options.services.disktree-web = {
    enable = lib.mkEnableOption "disktree's browser treemap";

    paths = lib.mkOption {
      type = lib.types.listOf absolute;
      default = [ "/" ];
      example = lib.literalExpression ''[ "/" "/var" "/home" ]'';
      description = ''
        Absolute directories to measure. Each one is walked on its own
        filesystem, so a directory on another mount is not entered unless
        it is listed here too. The default is the root directory.
      '';
    };

    exclude = lib.mkOption {
      type = lib.types.listOf absolute;
      default = [ ];
      example = lib.literalExpression ''[ "/nix/store" ]'';
      description = ''
        Absolute directories to skip. A child equal to one of these, or
        inside one, is not walked.
      '';
    };

    pacedPaths = lib.mkOption {
      type = lib.types.listOf absolute;
      default = [ ];
      example = lib.literalExpression ''[ "/var/lib/docker" ]'';
      description = ''
        After each directory under these prefixes, the walk pauses briefly.
        The service already runs in the idle I/O class.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8766;
      description = "Loopback port the treemap listens on.";
    };

    stateDirectory = lib.mkOption {
      type = absolute;
      default = "/var/lib/disktree-web";
      description = "Where the scan cache is stored.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.paths != [ ];
        message = "services.disktree-web.paths must list at least one directory.";
      }
    ];

    systemd.services.disktree-web = {
      description = "Web treemap of configured directories";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "local-fs.target" ];
      environment = {
        DISKTREE_STATE = cfg.stateDirectory;
        DISKTREE_PORT = toString cfg.port;
        DISKTREE_CONFIG = configFile;
      };
      serviceConfig = {
        ExecStart = "${pkgs.python3}/bin/python3 ${script}";
        Restart = "on-failure";
        RestartSec = "3s";
        Nice = 19;
        IOSchedulingClass = "idle";
      };
    };
  };
}
