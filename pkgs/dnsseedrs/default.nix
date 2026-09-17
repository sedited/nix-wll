{
  lib,
  rustPlatform,
  fetchFromGitHub,
  pkg-config,
  sqlite,
  darwin,
  stdenv,
}:

rustPlatform.buildRustPackage rec {
  pname = "dnsseedrs";
  version = "0.2.0";

  src = fetchFromGitHub {
    owner = "willcl-ark";
    repo = "dnsseedrs";
    rev = "2e8f7969405935c366afee6d889e255653c5bb57";
    hash = "sha256-znx7iAWDVIworDV9EB17I6zLME3fgiyTNtyo292aUCw=";
  };

  cargoHash = "sha256-IMAhmMENMbZO+S57atNHpibr46vSlj1g7zYeQ2Djo2w=";

  nativeBuildInputs = [ pkg-config ];
  buildInputs = [
    sqlite
  ]
  ++ lib.optionals stdenv.hostPlatform.isDarwin [
    darwin.apple_sdk.frameworks.Security
    darwin.apple_sdk.frameworks.SystemConfiguration
  ];

  meta = {
    description = "Bitcoin DNS seeder";
    homepage = "https://github.com/willcl-ark/dnsseedrs";
    mainProgram = "dnsseedrs";
    license = lib.licenses.mit;
  };
}
