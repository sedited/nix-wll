{
  services.ralph = {
    enable = true;
    origin = "https://git.example.org/owner/repo.git";
    repository = "owner/repo";
    forgejoApi = "https://git.example.org/api/v1/repos/owner/repo";
    botLogin = "ralph";

    openaiKeyFile = "/run/secrets/ralph/openai-key";
    ppqKeyFile = "/run/secrets/ralph/ppq-key";
    webhookSecretFile = "/run/secrets/ralph/webhook-secret";
    forgejoTokenFile = "/run/secrets/ralph/forgejo-token";
  };
}
