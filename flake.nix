{
  description = "On-demand background-removal virtual webcam (Robust Video Matting + v4l2loopback)";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";

      pkgsFor =
        {
          system,
          cudaCapabilities,
        }:
        import nixpkgs {
          inherit system;
          config = {
            allowUnfree = true;
            inherit cudaCapabilities;
            cudaForwardCompat = false;
          };
        };

      pkgs = pkgsFor {
        inherit system;
        cudaCapabilities = [ "8.6" ];
      };
    in
    {
      lib.mkPackage =
        {
          system ? "x86_64-linux",
          cudaCapabilities ? [ "8.6" ],
        }:
        (pkgsFor { inherit system cudaCapabilities; }).callPackage ./package.nix { };

      packages.${system}.default = self.lib.mkPackage { inherit system; };

      nixosModules.default = import ./module.nix self;

      devShells.${system}.default = pkgs.mkShell {
        packages = [
          self.packages.${system}.default.python
          pkgs.v4l-utils
          pkgs.ffmpeg
        ];
        RVM_MODEL_DIR = "${self.packages.${system}.default}/share/rvm-cam/models";
      };

      formatter.${system} = pkgs.nixfmt;
    };
}
