{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.bitcoinDnsSeed;
  defaultPackage = pkgs.callPackage ../../../pkgs/dnsseedrs { };

  mkDnssecSecrets =
    network: files:
    lib.listToAttrs (
      map (file: {
        name = "dnssec-${network}/${builtins.baseNameOf file}";
        value = {
          sopsFile = file;
          format = "binary";
          owner = "dnsseedrs";
          group = "dnsseedrs";
        };
      }) files
    );

  mkDnsseedrsInstance = name: network: {
    enable = network.enable;
    package = cfg.package;
    chain = network.chain;
    seedDomain = network.seedDomain;
    serverName = cfg.serverName;
    soaRname = cfg.soaRname;
    seedNodes = network.seedNodes;
    threads = network.threads;
    crawlRate = network.crawlRate;
    dbFile = network.dbFile;
    dumpFile = network.dumpFile;
    onionProxy = cfg.proxies.onionProxy;
    i2pProxy = cfg.proxies.i2pProxy;
    bind = [
      "udp://127.0.0.1:${toString network.dnsPort}"
      "tcp://127.0.0.1:${toString network.dnsPort}"
    ];
    dnssecKeys = "/run/secrets/dnssec-${name}";
  };

  mkCorednsZone = network: ''
    ${network.seedDomain}:53 {
      bind ${lib.concatStringsSep " " cfg.coredns.bindAddresses}
      forward . 127.0.0.1:${toString network.dnsPort}
      any
      log
    }
  '';

  mkDumpVhost =
    network: domain:
    lib.nameValuePair domain {
      extraConfig = ''
        root * ${cfg.dataDir}/${network}
        file_server browse {
          hide *.db
          hide sqlite*
        }
      '';
    };
in
{
  imports = [
    ../../dnsseedrs/nixos
  ];

  options.services.bitcoinDnsSeed = {
    enable = lib.mkEnableOption "Bitcoin DNS seed deployment";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "pkgs.callPackage ../../../pkgs/dnsseedrs { }";
      description = "dnsseedrs package to run.";
    };

    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/dnsseedrs";
      description = "Root directory for dnsseedrs network state and public seed dumps.";
    };

    serverName = lib.mkOption {
      type = lib.types.str;
      description = "DNS server name advertised by dnsseedrs.";
    };

    soaRname = lib.mkOption {
      type = lib.types.str;
      description = "SOA responsible party value advertised by dnsseedrs.";
    };

    mainnet = lib.mkOption {
      type = lib.types.submodule {
        options = {
          enable = lib.mkEnableOption "mainnet DNS seed";

          chain = lib.mkOption {
            type = lib.types.str;
            default = "main";
            description = "dnsseedrs chain identifier.";
          };

          seedDomain = lib.mkOption {
            type = lib.types.str;
            default = "seed.bitcoin.fish.foo";
            description = "DNS name served by the mainnet seed.";
          };

          dnsPort = lib.mkOption {
            type = lib.types.port;
            default = 5353;
            description = "Local dnsseedrs DNS port for CoreDNS to forward to.";
          };

          seedNodes = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            default = [
              "54.68.82.186:8333"
              "185.141.60.36:8333"
              "23.175.0.220:8333"
            ];
            description = "Initial Bitcoin peer addresses used by dnsseedrs.";
          };

          threads = lib.mkOption {
            type = lib.types.ints.positive;
            default = 6;
            description = "Crawler thread count.";
          };

          crawlRate = lib.mkOption {
            type = lib.types.nullOr lib.types.ints.positive;
            default = null;
            description = "Maximum aggregate crawl attempts to start per second; unset derives a rate from threads.";
          };

          dbFile = lib.mkOption {
            type = lib.types.str;
            default = "sqlite.db";
            description = "dnsseedrs database file name.";
          };

          dumpFile = lib.mkOption {
            type = lib.types.str;
            default = "seeds.txt";
            description = "dnsseedrs seed dump file name.";
          };

          dnssecKeyFiles = lib.mkOption {
            type = lib.types.listOf lib.types.path;
            default = [ ];
            description = "Sops-encrypted DNSSEC key files for the mainnet seed.";
          };
        };
      };
      default = { };
      description = "Mainnet DNS seed settings.";
    };

    signet = lib.mkOption {
      type = lib.types.submodule {
        options = {
          enable = lib.mkEnableOption "signet DNS seed";

          chain = lib.mkOption {
            type = lib.types.str;
            default = "signet";
            description = "dnsseedrs chain identifier.";
          };

          seedDomain = lib.mkOption {
            type = lib.types.str;
            default = "seed.signet.bitcoin.fish.foo";
            description = "DNS name served by the signet seed.";
          };

          dnsPort = lib.mkOption {
            type = lib.types.port;
            default = 5454;
            description = "Local dnsseedrs DNS port for CoreDNS to forward to.";
          };

          seedNodes = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            default = [ ];
            description = "Initial Bitcoin peer addresses used by dnsseedrs.";
          };

          threads = lib.mkOption {
            type = lib.types.ints.positive;
            default = 6;
            description = "Crawler thread count.";
          };

          crawlRate = lib.mkOption {
            type = lib.types.nullOr lib.types.ints.positive;
            default = null;
            description = "Maximum aggregate crawl attempts to start per second; unset derives a rate from threads.";
          };

          dbFile = lib.mkOption {
            type = lib.types.str;
            default = "sqlite.db";
            description = "dnsseedrs database file name.";
          };

          dumpFile = lib.mkOption {
            type = lib.types.str;
            default = "seeds.txt";
            description = "dnsseedrs seed dump file name.";
          };

          dnssecKeyFiles = lib.mkOption {
            type = lib.types.listOf lib.types.path;
            default = [ ];
            description = "Sops-encrypted DNSSEC key files for the signet seed.";
          };
        };
      };
      default = { };
      description = "Signet DNS seed settings.";
    };

    proxies = {
      manageLocal = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable local Tor and I2P SOCKS proxies for dnsseedrs.";
      };

      onionProxy = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1:9050";
        description = "SOCKS proxy used for onion peer crawling.";
      };

      i2pProxy = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1:4447";
        description = "SOCKS proxy used for I2P peer crawling.";
      };
    };

    coredns = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Configure CoreDNS to forward seed zones to dnsseedrs.";
      };

      bindAddresses = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [
          "0.0.0.0"
          "::"
        ];
        description = "Addresses CoreDNS binds for public DNS service.";
      };

      openFirewall = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Open TCP and UDP port 53.";
      };
    };

    caddy = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Configure Caddy virtual hosts for public seed dumps.";
      };

      mainnetDomain = lib.mkOption {
        type = lib.types.str;
        default = "bitcoin.fish.foo";
        description = "Public HTTP host for mainnet seed dumps.";
      };

      signetDomain = lib.mkOption {
        type = lib.types.str;
        default = "signet.bitcoin.fish.foo";
        description = "Public HTTP host for signet seed dumps.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !cfg.mainnet.enable || cfg.mainnet.dnssecKeyFiles != [ ];
        message = "services.bitcoinDnsSeed.mainnet.dnssecKeyFiles must be set when mainnet is enabled.";
      }
      {
        assertion = !cfg.signet.enable || cfg.signet.dnssecKeyFiles != [ ];
        message = "services.bitcoinDnsSeed.signet.dnssecKeyFiles must be set when signet is enabled.";
      }
    ];

    networking.nameservers = lib.mkIf cfg.coredns.enable [
      "1.1.1.1"
      "8.8.8.8"
    ];
    services.resolved.enable = lib.mkIf cfg.coredns.enable false;

    networking.firewall = lib.mkIf (cfg.coredns.enable && cfg.coredns.openFirewall) {
      allowedTCPPorts = [ 53 ];
      allowedUDPPorts = [ 53 ];
    };

    users.users.dnsseedrs = {
      isSystemUser = true;
      group = "dnsseedrs";
      extraGroups = [ "keys" ];
    };
    users.groups.dnsseedrs = { };

    sops.secrets =
      mkDnssecSecrets "mainnet" cfg.mainnet.dnssecKeyFiles
      // mkDnssecSecrets "signet" cfg.signet.dnssecKeyFiles;

    services.tor = lib.mkIf cfg.proxies.manageLocal {
      enable = true;
      client.enable = true;
    };

    services.i2pd = lib.mkIf cfg.proxies.manageLocal {
      enable = true;
      proto.socksProxy.enable = true;
    };

    systemd.services.i2pd = lib.mkIf cfg.proxies.manageLocal {
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
    };

    services.caddy.virtualHosts = lib.mkIf cfg.caddy.enable (
      builtins.listToAttrs [
        (mkDumpVhost "mainnet" cfg.caddy.mainnetDomain)
        (mkDumpVhost "signet" cfg.caddy.signetDomain)
      ]
    );

    services.coredns = lib.mkIf cfg.coredns.enable {
      enable = true;
      config = ''
        ${mkCorednsZone cfg.mainnet}

        ${mkCorednsZone cfg.signet}

        .:53 {
          bind ${lib.concatStringsSep " " cfg.coredns.bindAddresses}
          template ANY ANY {
            rcode REFUSED
          }
          log
        }
      '';
    };

    services.dnsseedrs = {
      mainnet = mkDnsseedrsInstance "mainnet" cfg.mainnet;
      signet = mkDnsseedrsInstance "signet" cfg.signet;
    };

    systemd.services.dnsseedrs-mainnet = lib.mkIf cfg.mainnet.enable {
      after = [ "sops-install-secrets.service" ];
      wants = [ "sops-install-secrets.service" ];
    };
    systemd.services.dnsseedrs-signet = lib.mkIf cfg.signet.enable {
      after = [ "sops-install-secrets.service" ];
      wants = [ "sops-install-secrets.service" ];
    };
  };
}
