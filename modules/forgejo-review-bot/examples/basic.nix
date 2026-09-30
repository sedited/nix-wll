{
  services.forgejoReviewBot = {
    enable = true;
    origin = "https://git.example.org/owner/repo.git";
    repository = "owner/repo";
    forgejoApi = "https://git.example.org/api/v1/repos/owner/repo";
    botLogin = "review-bot";

    openaiKeyFile = "/run/secrets/forgejo-review-bot/openai-key";
    ppqKeyFile = "/run/secrets/forgejo-review-bot/ppq-key";
    webhookSecretFile = "/run/secrets/forgejo-review-bot/webhook-secret";
    forgejoTokenFile = "/run/secrets/forgejo-review-bot/forgejo-token";
  };
}
