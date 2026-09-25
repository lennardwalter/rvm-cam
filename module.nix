self:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.rvm-cam;
  inherit (lib) mkOption types;
in
{
  options.services.rvm-cam = {
    enable = lib.mkEnableOption "the on-demand RVM background-removal virtual webcam";

    cudaCapabilities = mkOption {
      type = types.listOf types.str;
      default = [ "8.6" ];
      description = ''
        CUDA compute capabilities to build the CUDA dependencies for.
        Keeping this to your GPU avoids compiling NVSHMEM/NCCL for every
        architecture. RTX 30xx = 8.6, RTX 40xx = 8.9.
      '';
    };

    package = mkOption {
      type = types.package;
      default = self.lib.mkPackage {
        inherit (pkgs.stdenv.hostPlatform) system;
        inherit (cfg) cudaCapabilities;
      };
      defaultText = lib.literalExpression "rvm-cam built for cfg.cudaCapabilities";
    };

    videoNr = mkOption {
      type = types.int;
      default = 10;
      description = "Number of the virtual device, /dev/video<videoNr>.";
    };

    label = mkOption {
      type = types.str;
      default = "RVM Camera";
      description = "Device name shown to applications.";
    };

    camera = mkOption {
      type = types.str;
      example = "/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920-video-index0";
      description = "Real webcam to read from. Prefer a stable /dev/v4l/by-id path.";
    };

    width = mkOption {
      type = types.int;
      default = 1280;
    };

    height = mkOption {
      type = types.int;
      default = 720;
    };

    fps = mkOption {
      type = types.int;
      default = 30;
    };

    model = mkOption {
      type = types.enum [
        "mobilenetv3"
        "resnet50"
      ];
      default = "mobilenetv3";
      description = "RVM variant. resnet50 has slightly cleaner edges at higher cost.";
    };

    downsampleRatio = mkOption {
      type = types.float;
      default = 0.375;
      description = "RVM downsample ratio; ~0.375 for 720p, ~0.25 for 1080p.";
    };

    background = mkOption {
      type = types.enum [
        "blur"
        "image"
        "color"
      ];
      default = "blur";
    };

    blur = mkOption {
      type = types.number;
      default = 24;
      description = "Background blur sigma in pixels.";
    };

    image = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = "Background image, used when background = \"image\".";
    };

    color = mkOption {
      type = types.str;
      default = "00b140";
      description = "Background colour as RRGGBB, used when background = \"color\".";
    };

    mirror = mkOption {
      type = types.bool;
      default = false;
    };

    linger = mkOption {
      type = types.int;
      default = 5;
      description = "Seconds to keep the webcam and GPU busy after the last reader left.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.background != "image" || cfg.image != null;
        message = "services.rvm-cam.image must be set when background = \"image\".";
      }
    ];

    boot.extraModulePackages = [ config.boot.kernelPackages.v4l2loopback ];
    boot.kernelModules = [ "v4l2loopback" ];
    # exclusive_caps: browsers only list the device while a writer is attached,
    # which the daemon always is.
    boot.extraModprobeConfig = ''
      options v4l2loopback video_nr=${toString cfg.videoNr} card_label="${cfg.label}" exclusive_caps=1
    '';

    systemd.user.services.rvm-cam = {
      description = "RVM background-removal virtual webcam";
      wantedBy = [ "default.target" ];
      unitConfig.StartLimitIntervalSec = 0;
      serviceConfig = {
        ExecStart = lib.escapeShellArgs (
          [
            (lib.getExe cfg.package)
            "daemon"
            "--device"
            "/dev/video${toString cfg.videoNr}"
            "--width"
            (toString cfg.width)
            "--height"
            (toString cfg.height)
            "--linger"
            (toString cfg.linger)
            "--"
            "--camera"
            cfg.camera
            "--fps"
            (toString cfg.fps)
            "--model"
            cfg.model
            "--downsample-ratio"
            (toString cfg.downsampleRatio)
            "--background"
            cfg.background
            "--blur"
            (toString cfg.blur)
            "--color"
            cfg.color
          ]
          ++ lib.optionals (cfg.image != null) [
            "--image"
            "${cfg.image}"
          ]
          ++ lib.optional cfg.mirror "--mirror"
        );
        Restart = "on-failure";
        RestartSec = 5;
      };
    };
  };
}
