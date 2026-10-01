{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.ralph;
  defaultPackage = pkgs.callPackage ../../../pkgs/ralph { };
  defaultMarker = "<!-- ralph:${cfg.repository} -->";
  commentMarker = if cfg.commentMarker == null then defaultMarker else cfg.commentMarker;
  repositoryUrl =
    if cfg.repositoryUrl != null then
      cfg.repositoryUrl
    else
      builtins.replaceStrings [ "/api/v1/repos/" ] [ "/" ] cfg.forgejoApi;
  args = [
    "--listen"
    cfg.listenAddress
    "--port"
    (toString cfg.port)
    "--workers"
    (toString cfg.workers)
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
    "--review-budget-usd"
    (toString cfg.reviewBudgetUsd)
    "--ppq-review-budget-usd"
    (toString cfg.ppqReviewBudgetUsd)
    "--routing-mode"
    cfg.routingMode
    "--openai-key-file"
    cfg.openaiKeyFile
    "--ppq-key-file"
    cfg.ppqKeyFile
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
  ]
  ++ lib.optionals (cfg.modelsJson != null) [
    "--models-json"
    cfg.modelsJson
  ]
  ++ lib.optionals (cfg.reportDir != null && cfg.reportBaseUrl != null) [
    "--report-dir"
    cfg.reportDir
    "--report-base-url"
    cfg.reportBaseUrl
  ]
  ++ lib.optionals (cfg.monthlyBudgetUsd != null) [
    "--monthly-budget-usd"
    (toString cfg.monthlyBudgetUsd)
  ];
in
{
  options.services.ralph = {
    enable = lib.mkEnableOption "ralph pull request reviewer";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "pkgs.callPackage ../../../pkgs/ralph { }";
      description = "ralph package to run.";
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
      defaultText = lib.literalExpression "\"<!-- ralph:\${config.services.ralph.repository} -->\"";
      description = "Hidden marker used to find and update ralph's existing comment.";
    };

    promptFile = lib.mkOption {
      type = lib.types.path;
      default = "${cfg.package}/share/ralph/prompt.md";
      defaultText = lib.literalExpression "\"\${config.services.ralph.package}/share/ralph/prompt.md\"";
      description = "Markdown file containing the review prompt.";
    };

    auditPromptDir = lib.mkOption {
      type = lib.types.path;
      default = "${cfg.package}/share/ralph/audits";
      defaultText = lib.literalExpression "\"\${config.services.ralph.package}/share/ralph/audits\"";
      description = "Directory containing review stage prompts and models.json.";
    };

    modelsJson = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Optional replacement for auditPromptDir/models.json.";
    };

    workers = lib.mkOption {
      type = lib.types.ints.positive;
      default = 3;
      description = "Maximum concurrent PR reviews. Reviews of the same PR run serially.";
    };

    reviewBudgetUsd = lib.mkOption {
      type = lib.types.addCheck lib.types.number (value: value > 0);
      default = 1.00;
      description = ''
        Per-review spending ceiling in USD. Each API request reserves a
        conservative estimate before starting. Reviews that exhaust their
        allowance report incomplete coverage.
        Reservations are estimates, not an invoice cap.
      '';
    };

    ppqReviewBudgetUsd = lib.mkOption {
      type = lib.types.addCheck lib.types.number (value: value > 0);
      default = 0.50;
      description = "Separate per-review spending ceiling in USD for the PPQ GLM pass.";
    };

    monthlyBudgetUsd = lib.mkOption {
      type = lib.types.nullOr (lib.types.addCheck lib.types.number (value: value > 0));
      default = null;
      description = "Optional monthly API allowance in USD, shared by reviews in this state directory.";
    };

    routingMode = lib.mkOption {
      type = lib.types.enum [
        "enabled"
        "shadow"
        "full"
      ];
      default = "enabled";
      description = ''
        Select audits with conservative Luna routing, record routing decisions
        while running the full review in shadow mode, or always run the full
        review. All modes respect the review allowance.
      '';
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
      default = "/var/lib/ralph";
      description = "Private directory containing Git objects, durable jobs, review traces and the spend ledger.";
    };

    reportDir = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Public directory for full HTML and JSON review reports. Serve it with a web server; keep it separate from stateDir.";
    };

    reportBaseUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Public HTTP URL serving reportDir. Enables full report links in review comments.";
    };

    openaiKeyFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to a file containing the OpenAI API key.";
    };

    ppqKeyFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to a file containing the PPQ API key for GLM adversarial review.";
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
      default = "ralph";
      description = "System user running ralph.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "ralph";
      description = "System group running ralph.";
    };

  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.hasPrefix "/var/lib/" cfg.stateDir;
        message = "services.ralph.stateDir must be under /var/lib so systemd can manage it with StateDirectory.";
      }
      {
        assertion = (cfg.reportDir == null) == (cfg.reportBaseUrl == null);
        message = "services.ralph.reportDir and reportBaseUrl must be set together.";
      }
      {
        assertion =
          cfg.reportDir == null
          || (
            lib.hasPrefix "/" cfg.reportDir
            && (
              let
                reportDir = toString (builtins.toPath cfg.reportDir);
                stateDir = toString (builtins.toPath cfg.stateDir);
              in
              reportDir != "/"
              && reportDir != stateDir
              && !(lib.hasPrefix "${stateDir}/" reportDir)
              && !(lib.hasPrefix "${reportDir}/" stateDir)
            )
          );
        message = "services.ralph.reportDir must be absolute and separate from the private stateDir.";
      }
    ];

    systemd.tmpfiles.rules = lib.optional (
      cfg.reportDir != null
    ) "d ${cfg.reportDir} 0755 ${cfg.user} ${cfg.group} -";

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.stateDir;
    };
    users.groups.${cfg.group} = { };

    systemd.services.ralph-stats = lib.mkIf (cfg.reportDir != null && cfg.reportBaseUrl != null) {
      description = "Publish ralph review statistics";
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        ExecStart = "${cfg.package}/bin/ralph-evaluate ${
          lib.escapeShellArgs [
            "stats"
            "--state-dir"
            cfg.stateDir
            "--report-dir"
            cfg.reportDir
            "--repository-url"
            repositoryUrl
            "--report-base-url"
            cfg.reportBaseUrl
          ]
        }";
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.reportDir ];
        NoNewPrivileges = true;
        PrivateTmp = true;
      };
    };

    systemd.timers.ralph-stats = lib.mkIf (cfg.reportDir != null && cfg.reportBaseUrl != null) {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "1m";
        OnUnitActiveSec = "5m";
      };
    };

    systemd.services.ralph = {
      description = "ralph pull request reviewer";
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
        ReadWritePaths = [ cfg.stateDir ] ++ lib.optional (cfg.reportDir != null) cfg.reportDir;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        RestrictNamespaces = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        RestrictRealtime = true;
      };
    };
  };
}
