# Bitcoin DNS Seed NixOS Module

Reusable NixOS module for a Bitcoin DNS seed deployment built around
`dnsseedrs`, CoreDNS, DNSSEC key deployment, optional local Tor/I2P proxies, and
Caddy seed-dump hosting.

Callers provide site-local identity, domains, and encrypted DNSSEC key files.
The module owns the dnsseedrs, CoreDNS, proxy, firewall, Caddy virtual-host, and
systemd ordering details.

## Usage

```nix
{
  inputs.will-nix.url = "github:willcl-ark/nix";

  outputs =
    { nixpkgs, sops-nix, will-nix, ... }:
    {
      nixosConfigurations.host = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          sops-nix.nixosModules.sops
          will-nix.nixosModules.bitcoin-dnsseed
          ./host.nix
        ];
      };
    };
}
```

```nix
{
  services.bitcoinDnsSeed = {
    enable = true;
    serverName = "ns.example.org";
    soaRname = "admin.example.org";

    mainnet = {
      enable = true;
      seedDomain = "seed.bitcoin.example.org";
      dnssecKeyFiles = [
        ./secrets/mainnet/Kseed.example.+013+12345.key
        ./secrets/mainnet/Kseed.example.+013+12345.private
      ];
    };
  };
}
```

## Interface

- `services.bitcoinDnsSeed.enable`
- `services.bitcoinDnsSeed.package`
- `services.bitcoinDnsSeed.dataDir`
- `services.bitcoinDnsSeed.serverName`
- `services.bitcoinDnsSeed.soaRname`
- `services.bitcoinDnsSeed.mainnet.*`
- `services.bitcoinDnsSeed.signet.*`
- `services.bitcoinDnsSeed.proxies.*`
- `services.bitcoinDnsSeed.coredns.*`
- `services.bitcoinDnsSeed.caddy.*`

The `mainnet.crawlRate` and `signet.crawlRate` options cap aggregate crawl
attempts per second. Leave them unset to use dnsseedrs' adaptive default based
on the configured thread count.
