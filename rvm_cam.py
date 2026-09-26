#!/usr/bin/env python3
"""On-demand background-matting virtual webcam (Robust Video Matting + v4l2loopback).

daemon: owns the v4l2loopback device, emits a cheap placeholder frame and
        waits for a reader to STREAMON (v4l2loopback client-usage event).
        Only then it spawns a worker; when the last reader stops, the worker
        is killed, which releases the real webcam and the CUDA context
        (so the dGPU can power down again).
worker: real webcam -> RVM (CUDA, fp16) -> composite -> YUYV on stdout.
"""
import argparse
import fcntl
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- v4l2 bits
VIDIOC_S_FMT = 0xC0D05605
VIDIOC_SUBSCRIBE_EVENT = 0x4020565A
VIDIOC_DQEVENT = 0x80885659
V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_FIELD_NONE = 1
V4L2_COLORSPACE_SMPTE170M = 1
V4L2_PIX_FMT_YUYV = int.from_bytes(b"YUYV", "little")
# v4l2loopback.c: V4L2_EVENT_PRIVATE_START + 0x08E00000 + 1
V4L2_EVENT_PRI_CLIENT_USAGE = 0x08000000 + 0x08E00000 + 1


def set_output_format(fd, w, h):
    fmt = struct.pack(
        "I4x12I152x",
        V4L2_BUF_TYPE_VIDEO_OUTPUT,
        w, h, V4L2_PIX_FMT_YUYV, V4L2_FIELD_NONE,
        w * 2, w * h * 2, V4L2_COLORSPACE_SMPTE170M,
        0, 0, 0, 0, 0,
    )
    fcntl.ioctl(fd, VIDIOC_S_FMT, bytearray(fmt))


def subscribe_usage(fd):
    sub = struct.pack("III20x", V4L2_EVENT_PRI_CLIENT_USAGE, 0, 0)
    fcntl.ioctl(fd, VIDIOC_SUBSCRIBE_EVENT, bytearray(sub))


def dequeue_usage(fd):
    """Drain pending events, return last reader count (or None)."""
    count = None
    while True:
        buf = bytearray(136)
        try:
            fcntl.ioctl(fd, VIDIOC_DQEVENT, buf)
        except OSError:
            return count
        etype, = struct.unpack_from("I", buf, 0)
        if etype == V4L2_EVENT_PRI_CLIENT_USAGE:
            count, = struct.unpack_from("I", buf, 8)


def write_frame(fd, frame):
    try:
        os.write(fd, frame)
    except BlockingIOError:
        pass  # drop the frame rather than stall


def placeholder(w, h):
    # dark grey YUYV frame
    return bytes([32, 128]) * (w * h)


# -------------------------------------------------------------------- daemon
def daemon(args):
    w, h = args.width, args.height
    frame_size = w * h * 2
    idle = placeholder(w, h)

    fd = os.open(args.device, os.O_RDWR | os.O_NONBLOCK)
    set_output_format(fd, w, h)
    subscribe_usage(fd)
    os.write(fd, idle)
    log(f"daemon: {args.device} {w}x{h} ready, waiting for readers")

    worker = None
    buf = bytearray()
    readers = 0
    last_reader_at = 0.0
    next_spawn_at = 0.0
    last_write = 0.0
    # latest real frame; repeated while the worker is slower than the fill rate,
    # so the placeholder never flickers into a running stream
    last_frame = None
    spawned_at = 0.0

    def stop_worker():
        nonlocal worker, buf, last_frame
        last_frame = None
        if worker is None:
            return
        log("daemon: stopping worker")
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
        worker = None
        buf = bytearray()

    def shutdown(*_):
        stop_worker()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    worker_cmd = [sys.executable, os.path.abspath(__file__), "worker"] + args.worker_args

    while True:
        now = time.monotonic()
        if readers > 0:
            last_reader_at = now
        want = readers > 0 or (now - last_reader_at) < args.linger

        if want and worker is None and now >= next_spawn_at:
            log("daemon: reader active, starting worker")
            worker = subprocess.Popen(worker_cmd, stdout=subprocess.PIPE, bufsize=0)
            os.set_blocking(worker.stdout.fileno(), False)
            spawned_at = now
        elif not want and worker is not None:
            stop_worker()

        if worker is not None and worker.poll() is not None:
            log(f"daemon: worker exited with {worker.returncode}, retrying in 5s")
            worker = None
            buf = bytearray()
            last_frame = None
            next_spawn_at = now + 5

        p = select.poll()
        p.register(fd, select.POLLPRI)
        if worker is not None:
            p.register(worker.stdout.fileno(), select.POLLIN)
        # while someone watches, keep frames flowing at >= ~10fps
        timeout = 100 if readers > 0 else 1000
        for rfd, ev in p.poll(timeout):
            if rfd == fd:
                c = dequeue_usage(fd)
                if c is not None and c != readers:
                    log(f"daemon: readers {readers} -> {c}")
                    readers = c
            elif ev & select.POLLIN:
                try:
                    chunk = os.read(rfd, 1 << 22)
                except BlockingIOError:
                    chunk = b""
                buf += chunk
                if len(buf) >= frame_size:
                    n = len(buf) // frame_size
                    frame = bytes(buf[(n - 1) * frame_size:n * frame_size])
                    del buf[:n * frame_size]
                    if last_frame is None:
                        log(f"daemon: first frame after {time.monotonic() - spawned_at:.1f}s")
                    last_frame = frame
                    write_frame(fd, frame)
                    last_write = time.monotonic()

        if time.monotonic() - last_write > (0.1 if readers > 0 else 1.0):
            write_frame(fd, last_frame or idle)
            last_write = time.monotonic()


# -------------------------------------------------------------------- worker
class Camera:
    """Grabs frames in a thread so capture latency overlaps GPU work."""

    def __init__(self, dev, w, h, fps):
        import cv2
        self.cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise SystemExit(f"cannot open camera {dev}")
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.frame = None
        self.seq = 0
        self.cond = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            ok, f = self.cap.read()
            if not ok:
                log("worker: camera read failed")
                os._exit(3)
            with self.cond:
                self.frame, self.seq = f, self.seq + 1
                self.cond.notify()

    def read(self, last_seq):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq)
            return self.frame, self.seq


def resolve_model(model):
    """Accept a path or a bundled model name (mobilenetv3, resnet50)."""
    if os.path.isfile(model):
        return model
    path = os.path.join(os.environ.get("RVM_MODEL_DIR", ""), f"rvm_{model}_fp16.torchscript")
    if not os.path.isfile(path):
        raise SystemExit(f"model {model!r} not found (tried {path})")
    return path


def worker(args):
    import numpy as np
    import torch
    import torch.nn.functional as F

    dev = torch.device("cuda")
    model_path = resolve_model(args.model)
    model = torch.jit.load(model_path, map_location=dev).eval()
    dtype = torch.float16 if "fp16" in os.path.basename(model_path) else torch.float32

    W, H = args.width, args.height
    cam = Camera(args.camera, W, H, args.fps)

    bg_img = None
    if args.background == "image":
        import cv2
        img = cv2.imread(args.image)
        if img is None:
            raise SystemExit(f"cannot read {args.image}")
        img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
        bg_img = torch.from_numpy(img).to(dev).permute(2, 0, 1)[None].flip(1).to(dtype) / 255
    elif args.background == "color":
        r, g, b = (int(args.color[i:i + 2], 16) / 255 for i in (0, 2, 4))
        bg_img = torch.tensor([r, g, b], device=dev, dtype=dtype).view(1, 3, 1, 1).expand(1, 3, H, W)

    def gauss_kernel(sigma):
        k = int(sigma * 3) * 2 + 1
        x = torch.arange(k, device=dev, dtype=dtype) - k // 2
        g = torch.exp(-(x.float() ** 2) / (2 * sigma ** 2)).to(dtype)
        return g / g.sum()

    ds = 8  # blur at 1/8 resolution, cheap and plenty strong
    g = gauss_kernel(max(args.blur / ds, 0.5))
    gh = g.view(1, 1, 1, -1).repeat(4, 1, 1, 1)
    gv = g.view(1, 1, -1, 1).repeat(4, 1, 1, 1)

    def blur_bg(src, pha):
        # masked blur: average only background pixels, so the person
        # does not bleed a halo into the blurred background
        bgw = 1 - pha
        x = torch.cat([src * bgw, bgw], 1)
        x = F.interpolate(x, scale_factor=1 / ds, mode="area")
        pad = g.numel() // 2
        x = F.conv2d(F.pad(x, (pad, pad, 0, 0), mode="replicate"), gh, groups=4)
        x = F.conv2d(F.pad(x, (0, 0, pad, pad), mode="replicate"), gv, groups=4)
        x = F.interpolate(x, size=(H, W), mode="bilinear", align_corners=False)
        return x[:, :3] / x[:, 3:].clamp_min(1e-3)

    # BT.601 limited range, rgb in [0,1]
    M = torch.tensor([[65.481, 128.553, 24.966],
                      [-37.797, -74.203, 112.0],
                      [112.0, -93.786, -18.214]], device=dev)
    off = torch.tensor([16.0, 128.0, 128.0], device=dev).view(3, 1, 1)

    def to_yuyv(rgb):
        yuv = torch.einsum("ij,jhw->ihw", M, rgb[0].float()) + off
        y = yuv[0]
        uv = yuv[1:].view(2, H, W // 2, 2).mean(-1)
        out = torch.stack([y[:, 0::2], uv[0], y[:, 1::2], uv[1]], -1)
        return out.round_().clamp_(0, 255).to(torch.uint8).reshape(-1)

    out_pinned = torch.empty(W * H * 2, dtype=torch.uint8, pin_memory=True)
    stdout = sys.stdout.buffer
    rec = [None] * 4
    seq = 0
    frames, t0 = 0, time.monotonic()
    log(f"worker: {torch.cuda.get_device_name()} model={os.path.basename(model_path)} bg={args.background}")

    with torch.inference_mode():
        while True:
            frame, seq = cam.read(seq)
            if frame.shape[0] != H or frame.shape[1] != W:
                import cv2
                frame = cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA)
            src = torch.from_numpy(frame).to(dev, non_blocking=True)
            src = src.permute(2, 0, 1)[None].flip(1).to(dtype) / 255  # BGR->RGB
            if args.mirror:
                src = src.flip(3)
            fgr, pha, *rec = model(src, *rec, args.downsample_ratio)
            if bg_img is None:
                com = src * pha + blur_bg(src, pha) * (1 - pha)
            else:
                com = fgr * pha + bg_img * (1 - pha)
            out_pinned.copy_(to_yuyv(com.clamp(0, 1)))
            if args.bench:
                torch.cuda.synchronize()
            else:
                stdout.write(memoryview(out_pinned.numpy()))
                stdout.flush()
            frames += 1
            if args.bench and time.monotonic() - t0 >= 5:
                dt = time.monotonic() - t0
                log(f"worker: {frames / dt:.1f} fps, "
                    f"vram {torch.cuda.max_memory_allocated() / 2**20:.0f} MiB")
                frames, t0 = 0, time.monotonic()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daemon")
    d.add_argument("--device", default="/dev/video10")
    d.add_argument("--linger", type=float, default=5.0,
                   help="seconds to keep the worker after last reader left")
    wk = sub.add_parser("worker")
    wk.add_argument("--camera", default="/dev/video0")
    wk.add_argument("--model", default="mobilenetv3",
                    help="mobilenetv3, resnet50 or a path to an RVM torchscript file")
    wk.add_argument("--downsample-ratio", type=float, default=0.375)
    wk.add_argument("--background", choices=["blur", "image", "color"], default="blur")
    wk.add_argument("--blur", type=float, default=24, help="blur sigma in px")
    wk.add_argument("--image")
    wk.add_argument("--color", default="00b140")
    wk.add_argument("--mirror", action="store_true")
    wk.add_argument("--fps", type=int, default=30)
    wk.add_argument("--bench", action="store_true")
    for p in (d, wk):
        p.add_argument("--width", type=int, default=1280)
        p.add_argument("--height", type=int, default=720)
    args, rest = ap.parse_known_args()
    if args.cmd == "daemon":
        # everything after "--" is forwarded to the worker
        args.worker_args = [a for a in rest if a != "--"] + [
            "--width", str(args.width), "--height", str(args.height)]
        daemon(args)
    else:
        if rest:
            ap.error(f"unknown args {rest}")
        worker(args)


if __name__ == "__main__":
    main()
