{
  lib,
  stdenvNoCC,
  python3,
  git,
  makeWrapper,
}:

stdenvNoCC.mkDerivation {
  pname = "forgejo-review-bot";
  version = "0.1.0";

  src = ./.;

  nativeBuildInputs = [
    git
    makeWrapper
    python3
  ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    export HOME="$TMPDIR"
    ${python3.interpreter} -m unittest discover -s . -p 'test_*.py'
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 bot.py $out/libexec/forgejo-review-bot/bot.py
    makeWrapper ${python3.interpreter} $out/bin/forgejo-review-bot \
      --add-flags $out/libexec/forgejo-review-bot/bot.py \
      --prefix PATH : ${lib.makeBinPath [ git ]}
    runHook postInstall
  '';

  meta = {
    description = "Forgejo pull request first-pass review bot";
    mainProgram = "forgejo-review-bot";
    platforms = lib.platforms.unix;
  };
}
