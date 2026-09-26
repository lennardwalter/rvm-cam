{
  lib,
  stdenvNoCC,
  python3,
  fetchurl,
  makeWrapper,
}:

let
  # torch-bin's wheel metadata asks for the pip nvidia-*-cu12 packages; nixpkgs
  # provides those libraries itself, so the metadata check is a false negative.
  torch = python3.pkgs.torch-bin.overridePythonAttrs { dontCheckRuntimeDeps = true; };

  python = python3.withPackages (ps: [
    torch
    ps.opencv4
    ps.numpy
  ]);

  fetchModel =
    name: hash:
    fetchurl {
      url = "https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_${name}_fp16.torchscript";
      inherit hash;
    };

  models = {
    mobilenetv3 = fetchModel "mobilenetv3" "sha256-hHqLUTlJivv3q96cw0e0EDBUD2SY21k6Dj2d3h7M3ZY=";
    resnet50 = fetchModel "resnet50" "sha256-EnPlinlGKWsUiESnO4fjk9CsjlvOBK+HftRDaG48fEY=";
  };
in
stdenvNoCC.mkDerivation {
  pname = "rvm-cam";
  version = "0.1.0";

  src = ./rvm_cam.py;
  dontUnpack = true;

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall

    install -Dm644 $src $out/share/rvm-cam/rvm_cam.py
    mkdir -p $out/share/rvm-cam/models
    ${lib.concatStrings (
      lib.mapAttrsToList (name: model: ''
        ln -s ${model} $out/share/rvm-cam/models/rvm_${name}_fp16.torchscript
      '') models
    )}
    makeWrapper ${python.interpreter} $out/bin/rvm-cam \
      --add-flags $out/share/rvm-cam/rvm_cam.py \
      --set-default RVM_MODEL_DIR $out/share/rvm-cam/models

    runHook postInstall
  '';

  passthru = { inherit python models; };

  meta = {
    description = "On-demand background-removal virtual webcam (Robust Video Matting + v4l2loopback)";
    license = lib.licenses.mit;
    platforms = [ "x86_64-linux" ];
    mainProgram = "rvm-cam";
  };
}
