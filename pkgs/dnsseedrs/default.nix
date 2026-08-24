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
    rev = "160687a52863c7e7492bbec970b24c3ab904e45e";
    hash = "sha256-e/aFYpzTkYX3DZJgvBjaWuf6nunACifDMGQ9eiqE9g8=";
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
