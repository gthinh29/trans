# To learn more about how to use Nix to configure your environment
# see: https://firebase.google.com/docs/studio/customize-workspace
{ pkgs, ... }: {
  # Which nixpkgs channel to use.
  channel = "stable-24.05"; # or "unstable"

  # Use https://search.nixos.org/packages to find packages
  packages = [
    pkgs.go
    pkgs.python311
    pkgs.nodejs_20
    pkgs.nodePackages.nodemon
  ];

  idx = {
    # Search for the extensions you want on https://open-vsx.org/ and use "publisher.id"
    extensions = [];
    # Enable previews
    previews = {
      enable = true;
      previews = {};
    };
    # Workspace lifecycle hooks
    workspace = {};
  };
}
