{ pkgs }:

{
  dnsseedrs = pkgs.callPackage ./dnsseedrs { };
  forgejo-review-bot = pkgs.callPackage ./forgejo-review-bot { };
}
