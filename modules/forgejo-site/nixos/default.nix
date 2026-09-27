{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.forgejoSite;
  forgejo = lib.getExe cfg.package;
  forgejoUrl = "http://${cfg.http.address}:${toString cfg.http.port}";
  proxyTarget = if cfg.anubis.enable then cfg.anubis.bind else forgejoUrl;

  secretPath = name: config.sops.secrets.${name}.path;
  stateDir = config.services.forgejo.stateDir;

  adminInit = pkgs.writeShellScript "forgejo-admin-init" ''
    set -euo pipefail

    if ${forgejo} admin user list | ${pkgs.gawk}/bin/awk -v user=${lib.escapeShellArg cfg.admin.user} 'NR > 1 && $2 == user { found = 1 } END { exit found ? 0 : 1 }'; then
      exit 0
    fi

    password="$(cat ${lib.escapeShellArg (secretPath cfg.secrets.adminPassword)})"
    ${forgejo} admin user create \
      --admin \
      --username ${lib.escapeShellArg cfg.admin.user} \
      --password "$password" \
      --email ${lib.escapeShellArg cfg.admin.email} \
      --must-change-password=false
  '';
in
{
  options.services.forgejoSite = {
    enable = lib.mkEnableOption "Forgejo site deployment";

    domain = lib.mkOption {
      type = lib.types.str;
      description = "Public Forgejo domain.";
    };

    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/forgejo";
      description = "Forgejo state directory.";
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.forgejo;
      defaultText = lib.literalExpression "pkgs.forgejo";
      description = "Forgejo package to run.";
    };

    admin = {
      user = lib.mkOption {
        type = lib.types.str;
        description = "Initial Forgejo administrator username.";
      };

      email = lib.mkOption {
        type = lib.types.str;
        description = "Initial Forgejo administrator email address.";
      };
    };

    http = {
      address = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1";
        description = "Forgejo HTTP listen address.";
      };

      port = lib.mkOption {
        type = lib.types.port;
        default = 3001;
        description = "Forgejo HTTP listen port.";
      };
    };

    sshPort = lib.mkOption {
      type = lib.types.port;
      default = 22;
      description = "Public SSH port advertised by Forgejo.";
    };

    secrets = {
      adminPassword = lib.mkOption {
        type = lib.types.str;
        default = "forgejo-admin-password";
        description = "sops-nix secret name containing the initial admin password.";
      };

      secretKey = lib.mkOption {
        type = lib.types.str;
        default = "forgejo-secret-key";
        description = "sops-nix secret name containing Forgejo SECRET_KEY.";
      };

      internalToken = lib.mkOption {
        type = lib.types.str;
        default = "forgejo-internal-token";
        description = "sops-nix secret name containing Forgejo INTERNAL_TOKEN.";
      };

      oauth2JwtSecret = lib.mkOption {
        type = lib.types.str;
        default = "forgejo-oauth2-jwt-secret";
        description = "sops-nix secret name containing Forgejo OAuth2 JWT_SECRET.";
      };

      mailerPassword = lib.mkOption {
        type = lib.types.str;
        default = "forgejo-mailer-password";
        description = "sops-nix secret name containing the SMTP password.";
      };
    };

    mailer = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Enable SMTP mail delivery.";
      };

      from = lib.mkOption {
        type = lib.types.str;
        default = "Forgejo <forgejo@example.org>";
        description = "Mailer From header.";
      };

      protocol = lib.mkOption {
        type = lib.types.str;
        default = "smtps";
        description = "Forgejo mailer protocol.";
      };

      smtpAddress = lib.mkOption {
        type = lib.types.str;
        default = "localhost";
        description = "SMTP server address.";
      };

      smtpPort = lib.mkOption {
        type = lib.types.port;
        default = 465;
        description = "SMTP server port.";
      };

      user = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "SMTP username.";
      };
    };

    anubis = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Put Anubis in front of Forgejo.";
      };

      instanceName = lib.mkOption {
        type = lib.types.str;
        default = "forgejo";
        description = "Anubis instance name.";
      };

      bind = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1:3002";
        description = "Anubis bind address.";
      };

      ogPassthrough = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Allow Open Graph passthrough through Anubis.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    sops.secrets = {
      ${cfg.secrets.adminPassword} = {
        owner = "forgejo";
        group = "forgejo";
        mode = "0400";
      };
      ${cfg.secrets.secretKey} = {
        owner = "forgejo";
        group = "forgejo";
        mode = "0400";
      };
      ${cfg.secrets.internalToken} = {
        owner = "forgejo";
        group = "forgejo";
        mode = "0400";
      };
      ${cfg.secrets.oauth2JwtSecret} = {
        owner = "forgejo";
        group = "forgejo";
        mode = "0400";
      };
    }
    // lib.optionalAttrs cfg.mailer.enable {
      ${cfg.secrets.mailerPassword} = {
        owner = "forgejo";
        group = "forgejo";
        mode = "0400";
      };
    };

    services.forgejo = {
      enable = true;
      package = cfg.package;
      stateDir = cfg.dataDir;

      database.type = "sqlite3";
      dump.enable = true;
      lfs.enable = true;

      settings = {
        DEFAULT.APP_NAME = cfg.domain;

        server = {
          DOMAIN = cfg.domain;
          ROOT_URL = "https://${cfg.domain}/";
          HTTP_ADDR = cfg.http.address;
          HTTP_PORT = cfg.http.port;
          START_SSH_SERVER = false;
          DISABLE_SSH = false;
          SSH_DOMAIN = cfg.domain;
          SSH_PORT = cfg.sshPort;
          SSH_USER = "forgejo";
          OFFLINE_MODE = true;
        };

        session.COOKIE_SECURE = true;

        service = {
          DISABLE_REGISTRATION = true;
          REQUIRE_SIGNIN_VIEW = false;
          ENABLE_BASIC_AUTHENTICATION = false;
          ENABLE_NOTIFY_MAIL = cfg.mailer.enable;
          DEFAULT_ALLOW_CREATE_ORGANIZATION = false;
        };

        mailer = {
          ENABLED = cfg.mailer.enable;
        }
        // lib.optionalAttrs cfg.mailer.enable {
          FROM = cfg.mailer.from;
          PROTOCOL = cfg.mailer.protocol;
          SMTP_ADDR = cfg.mailer.smtpAddress;
          SMTP_PORT = cfg.mailer.smtpPort;
          USER = cfg.mailer.user;
          PASSWD_URI = "file:${secretPath cfg.secrets.mailerPassword}";
        };

        admin.DISABLE_REGULAR_ORG_CREATION = true;

        security = {
          GLOBAL_TWO_FACTOR_REQUIREMENT = "admin";
          LOGIN_REMEMBER_DAYS = 7;
          DISABLE_QUERY_AUTH_TOKEN = true;
        };

        repository = {
          DEFAULT_PRIVATE = "public";
          DEFAULT_BRANCH = "master";
          DISABLE_HTTP_GIT = false;
          ENABLE_PUSH_CREATE_USER = false;
          ENABLE_PUSH_CREATE_ORG = false;
          DISABLED_REPO_UNITS = "repo.packages";
          DEFAULT_REPO_UNITS = "repo.code,repo.releases,repo.issues,repo.pulls,repo.wiki,repo.projects,repo.actions";
          DEFAULT_MIRROR_REPO_UNITS = "repo.code,repo.releases,repo.issues,repo.pulls,repo.wiki,repo.projects,repo.actions";
          ALLOW_ADOPTION_OF_UNADOPTED_REPOSITORIES = false;
          ALLOW_DELETION_OF_UNADOPTED_REPOSITORIES = false;
          DISABLE_MIGRATIONS = false;
        };

        migrations = {
          ALLOWED_DOMAINS = "github.com,*.github.com";
          ALLOW_LOCALNETWORKS = false;
        };

        "repository.upload".ENABLED = false;
        actions.ENABLED = true;
        packages.ENABLED = false;
        oauth2.ENABLED = true;
        openid.ENABLE_OPENID_SIGNIN = false;
        openid.ENABLE_OPENID_SIGNUP = false;
        api.ENABLE_SWAGGER = false;
        log.LEVEL = "Info";
      };

      secrets = {
        security = {
          SECRET_KEY = lib.mkForce (secretPath cfg.secrets.secretKey);
          INTERNAL_TOKEN = lib.mkForce (secretPath cfg.secrets.internalToken);
        };
        oauth2.JWT_SECRET = lib.mkForce (secretPath cfg.secrets.oauth2JwtSecret);
      };
    };

    services.caddy.virtualHosts.${cfg.domain}.extraConfig = ''
      reverse_proxy ${proxyTarget} {
        header_up X-Forwarded-For {client_ip}
        header_up X-Real-IP {client_ip}
        header_up X-Http-Version {http.request.proto}
      }
    '';

    services.anubis.instances.${cfg.anubis.instanceName}.settings = lib.mkIf cfg.anubis.enable {
      TARGET = forgejoUrl;
      BIND = cfg.anubis.bind;
      BIND_NETWORK = "tcp";
      OG_PASSTHROUGH = cfg.anubis.ogPassthrough;
    };

    systemd.services.forgejo = {
      after = [ "sops-install-secrets.service" ];
      wants = [ "sops-install-secrets.service" ];
    };

    systemd.services.forgejo-admin-init = {
      description = "Create the initial Forgejo administrator";
      after = [
        "forgejo.service"
        "sops-install-secrets.service"
      ];
      requires = [ "forgejo.service" ];
      wants = [ "sops-install-secrets.service" ];
      wantedBy = [ "multi-user.target" ];
      path = [
        cfg.package
        pkgs.gawk
      ];
      environment = {
        USER = config.services.forgejo.user;
        HOME = stateDir;
        FORGEJO_WORK_DIR = stateDir;
        FORGEJO_CUSTOM = config.services.forgejo.customDir;
      };
      serviceConfig = {
        Type = "oneshot";
        User = config.services.forgejo.user;
        Group = config.services.forgejo.group;
        WorkingDirectory = stateDir;
        ExecStart = adminInit;
      };
    };
  };
}
