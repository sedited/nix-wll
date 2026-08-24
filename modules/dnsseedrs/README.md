# dnsseedrs NixOS Module

Generic multi-instance NixOS module for running `dnsseedrs`.

Use this module when you want direct control over one or more raw
`services.dnsseedrs` instances. Use `nixosModules.bitcoin-dnsseed` instead when
you want the higher-level deployment module that also configures CoreDNS,
DNSSEC secrets, Caddy seed dumps, and local Tor/I2P proxies.

## Usage

```nix
{
  services.dnsseedrs.mainnet = {
    enable = true;
    chain = "main";
    seedDomain = "seed.example.org";
    serverName = "ns.example.org";
    soaRname = "admin.example.org";
    seedNodes = [ "1.2.3.4:8333" ];
    crawlRate = 10;
    bind = [
      "udp://127.0.0.1:5353"
      "tcp://127.0.0.1:5353"
    ];
  };
}
```

## Interface

- `services.dnsseedrs.<name>.enable`
- `services.dnsseedrs.<name>.package`
- `services.dnsseedrs.<name>.chain`
- `services.dnsseedrs.<name>.seedDomain`
- `services.dnsseedrs.<name>.serverName`
- `services.dnsseedrs.<name>.soaRname`
- `services.dnsseedrs.<name>.seedNodes`
- `services.dnsseedrs.<name>.dbFile`
- `services.dnsseedrs.<name>.dumpFile`
- `services.dnsseedrs.<name>.bind`
- `services.dnsseedrs.<name>.dnssecKeys`
- `services.dnsseedrs.<name>.onionProxy`
- `services.dnsseedrs.<name>.i2pProxy`
- `services.dnsseedrs.<name>.threads`
- `services.dnsseedrs.<name>.crawlRate`
- `services.dnsseedrs.<name>.extraArgs`
