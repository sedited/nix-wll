{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.forgejoReviewBot;
  defaultPackage = pkgs.callPackage ../../../pkgs/forgejo-review-bot { };
  defaultMarker = "<!-- forgejo-review-bot:${cfg.repository} -->";
  commentMarker = if cfg.commentMarker == null then defaultMarker else cfg.commentMarker;
  args = [
    "--listen"
    cfg.listenAddress
    "--port"
    (toString cfg.port)
    "--state-dir"
    cfg.stateDir
    "--origin"
    cfg.origin
    "--repository"
    cfg.repository
    "--forgejo-api"
    cfg.forgejoApi
    "--comment-marker"
    commentMarker
    "--prompt-file"
    cfg.promptFile
    "--audit-prompt-dir"
    cfg.auditPromptDir
    "--openai-key-file"
    cfg.openaiKeyFile
    "--webhook-secret-file"
    cfg.webhookSecretFile
    "--forgejo-token-file"
    cfg.forgejoTokenFile
    "--bot-login"
    cfg.botLogin
  ]
  ++ lib.optionals (cfg.repositoryUrl != null) [
    "--repository-url"
    cfg.repositoryUrl
  ];
in
{
  options.services.forgejoReviewBot = {
    enable = lib.mkEnableOption "Forgejo pull request review bot";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "pkgs.callPackage ../../../pkgs/forgejo-review-bot { }";
      description = "forgejo-review-bot package to run.";
    };

    origin = lib.mkOption {
      type = lib.types.str;
      description = "Git remote URL used to fetch base branches and pull request heads.";
    };

    repository = lib.mkOption {
      type = lib.types.str;
      example = "owner/repo";
      description = "Forgejo repository full name accepted from webhook payloads.";
    };

    forgejoApi = lib.mkOption {
      type = lib.types.str;
      example = "https://git.example.org/api/v1/repos/owner/repo";
      description = "Forgejo repository API URL.";
    };

    repositoryUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "https://git.example.org/owner/repo";
      description = "Expected repository HTML URL in webhook payloads. Unset derives it from forgejoApi.";
    };

    commentMarker = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      defaultText = lib.literalExpression "\"<!-- forgejo-review-bot:\${config.services.forgejoReviewBot.repository} -->\"";
      description = "Hidden marker used to find and update the bot's existing comment.";
    };

    promptFile = lib.mkOption {
      type = lib.types.path;
      default = "${cfg.package}/share/forgejo-review-bot/prompt.md";
      defaultText = lib.literalExpression "\"\${config.services.forgejoReviewBot.package}/share/forgejo-review-bot/prompt.md\"";
      description = "Markdown file containing the review prompt.";
    };

    auditPromptDir = lib.mkOption {
      type = lib.types.path;
      default = "${cfg.package}/share/forgejo-review-bot/audits";
      defaultText = lib.literalExpression "\"\${config.services.forgejoReviewBot.package}/share/forgejo-review-bot/audits\"";
      description = "Directory containing review stage prompts and models.json.";
    };

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Address for the webhook HTTP server.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8765;
      description = "Port for the webhook HTTP server.";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/forgejo-review-bot";
      description = "Directory containing the bot checkout state.";
    };

    codexPackage = lib.mkOption {
      type = lib.types.package;
      description = "Codex CLI package used to run review stages.";
    };

    openaiKeyFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to a file containing the OpenAI API key.";
    };

    webhookSecretFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to a file containing the Forgejo webhook secret.";
    };

    forgejoTokenFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to a file containing the Forgejo API token.";
    };

    botLogin = lib.mkOption {
      type = lib.types.str;
      description = "Forgejo account login that owns the review comment.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "forgejo-review-bot";
      description = "System user running the review bot.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "forgejo-review-bot";
      description = "System group running the review bot.";
    };

  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.hasPrefix "/var/lib/" cfg.stateDir;
        message = "services.forgejoReviewBot.stateDir must be under /var/lib so systemd can manage it with StateDirectory.";
      }
    ];

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.stateDir;
    };
    users.groups.${cfg.group} = { };

    systemd.services.forgejo-review-bot = {
      description = "Forgejo pull request review bot";
      path = [ cfg.codexPackage pkgs.ripgrep ];
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} ${lib.escapeShellArgs args}";
        User = cfg.user;
        Group = cfg.group;
        StateDirectory = lib.removePrefix "/var/lib/" cfg.stateDir;
        StateDirectoryMode = "0700";
        WorkingDirectory = cfg.stateDir;

        Restart = "always";
        RestartSec = "10s";

        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.stateDir ];
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        RestrictNamespaces = "user mnt pid net";
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        RestrictRealtime = true;
      };
    };
  };
}
