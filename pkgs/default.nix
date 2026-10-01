{ pkgs }:

{
  dnsseedrs = pkgs.callPackage ./dnsseedrs { };
  ralph = pkgs.callPackage ./ralph { };
}
