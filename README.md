# rvm-cam

On-demand background-removal virtual webcam for Linux, packaged as a Nix flake.

- **Model:** [Robust Video Matting](https://github.com/PeterL1n/RobustVideoMatting)
  (MobileNetV3 or ResNet50, fp16 TorchScript) on CUDA. It's temporally stable and
  needs no first-frame mask or background shot.
- **Output:** a v4l2loopback device (`/dev/video10`, "RVM Camera") usable from
  browsers, Zoom, Teams, OBS, and so on.
- **On-demand:** the real webcam and the GPU are only used while some
  application is actually *streaming* from the virtual camera.

## How it works

```
rvm-cam daemon (always running, no torch, ~0 CPU)
  ├─ owns /dev/video10 as writer, emits a dark placeholder frame
  ├─ subscribes to v4l2loopback's V4L2_EVENT_PRI_CLIENT_USAGE event
  │    (fires on STREAMON/STREAMOFF of a reader, not on mere device enumeration)
  └─ reader appears → spawns   rvm-cam worker
                                 webcam (MJPG) → RVM (CUDA fp16) → composite
                                 → YUYV frames on stdout → daemon → /dev/video10
     last reader gone (+ linger seconds) → worker killed:
       webcam LED off, CUDA context gone, dGPU can runtime-suspend again
```

Background modes: `blur` (masked blur, no halo around you), `image`, `color`.

## Performance

Measured on an RTX 3060 Laptop (6 GB) at 1280x720, fp16:

| model       | inference | VRAM (worker) |
|-------------|-----------|---------------|
| mobilenetv3 | ~16 ms    | ~200 MiB      |
| resnet50    | ~35 ms    | more          |

In practice the webcam is usually the limit. Many webcams (e.g. the Logitech
C920) halve their frame rate in low light. `v4l2-ctl -d <cam> -c
exposure_dynamic_framerate=0` keeps them at 30 fps, at the cost of a darker
image.

## NixOS usage

```nix
# flake.nix
inputs.rvm-cam = {
  url = "github:<you>/rvm-cam";
  inputs.nixpkgs.follows = "nixpkgs";
};

# in your nixosSystem modules
inputs.rvm-cam.nixosModules.default
{
  services.rvm-cam = {
    enable = true;
    camera = "/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920-video-index0";
    cudaCapabilities = [ "8.6" ]; # RTX 30xx; 8.9 for RTX 40xx
    # background = "image"; image = ./wallpaper.jpg;
  };
}
```

This adds the v4l2loopback kernel module (loaded at boot, with `exclusive_caps=1`)
and a `systemd --user` service `rvm-cam.service`. Logs are in
`journalctl --user -u rvm-cam -f`.

Notes:

- Requires the proprietary NVIDIA driver (`hardware.nvidia`) and `allowUnfree`
  (the flake instantiates its own nixpkgs with `allowUnfree` for CUDA/torch).
- `torch-bin` depends on NVSHMEM/NCCL, which are not in any binary cache. They
  are compiled once for `cudaCapabilities` only, which keeps that step to minutes.
- If you also use v4l2loopback for something else (OBS virtual camera,
  DroidCam), the `options v4l2loopback` lines will conflict. Merge them
  into one line with `devices=N` and comma-separated per-device values.

## Manual use / development

```sh
nix develop
python rvm_cam.py worker --camera /dev/video0 --bench        # fps + VRAM
python rvm_cam.py daemon --device /dev/video10 -- --camera /dev/video0
ffplay /dev/video10                                          # triggers the worker
```

Temporary loopback device without touching the system config:

```sh
sudo insmod $(nix build --no-link --print-out-paths nixpkgs#linuxPackages_latest.v4l2loopback)/lib/modules/*/updates/v4l2loopback.ko.xz \
  video_nr=10 card_label="RVM Camera" exclusive_caps=1
```

(use the v4l2loopback built for your running kernel, e.g. from
`config.boot.kernelPackages` of your system).

## License

The RVM model weights are GPL-3.0 (downloaded at build time from the upstream
release). No license has been chosen for this repository's own code yet.
