{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
  };

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      # fhsEnv = pkgs.buildFHSEnv {
      #   name = "python-fhs";
      #   targetPkgs =
      #     pkgs: with pkgs; [
      #       python312
      #       python312Packages.pip
      #       python312Packages.virtualenv

      #       pkg-config

      #       gflags
      #       zlib
      #       bzip2
      #       lz4
      #       snappy
      #       zstd
      #     ];
      #   runScript = "bash";
      # };
    in
    {
      # devShells.${system}.default = fhsEnv.env;
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          python312
          python312Packages.pip
          python312Packages.virtualenv

          python312Packages.matplotlib
          python312Packages.pandas
          python312Packages.seaborn
          # python312Packages.pyqt6

          pkg-config
          gflags
          snappy
          zlib
          bzip2
          lz4
          zstd

          elfutils
          llvmPackages.clang-unwrapped
          llvmPackages.llvm

          linuxHeaders
          # linuxPackages.kernel.dev
        ];

        env.LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath (
          with pkgs;
          [
            stdenv.cc.cc.lib
            gflags
            zlib
            bzip2
            lz4
            snappy
            zstd

            # BCC
          ]
        );

        shellHook = ''
          export CFLAGS="$CFLAGS -Wno-error=format-truncation -Wno-format-truncation"
          export CXXFLAGS="$CXXFLAGS -Wno-error=format-truncation -Wno-format-truncation"

          # make kernel headers visible to clang/gcc (helps libbpf builds find asm-generic/)
          export C_INCLUDE_PATH="${pkgs.linuxHeaders}/include:$C_INCLUDE_PATH"
          export CPLUS_INCLUDE_PATH="${pkgs.linuxHeaders}/include:$CPLUS_INCLUDE_PATH"
        '';
      };
    };
}
