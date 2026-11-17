# Control-Plane Declaration

The control-plane declaration (`ControlPlaneConfiguration`) defines the control plane's networking, PKI, server certificate, boot policy, authentication policy, registration window and credential rotation. It is the only input for these parameters; dnsmasq configuration and PKI are derived from it at startup.

```yaml
apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: kurrent-cp
spec:
  networking:        # Required: five deployment facts
    interface: enp3s0
    subnet: 192.168.80.0/24
    dhcpRange: 192.168.80.50,192.168.80.100
    gateway: 192.168.80.2
    dns: 223.5.5.5
  pki: {}            # Bootstrap tokens, component certificates, renew threshold, CRL period
  serverCert: {}     # Server certificate SAN/days, whether to rotate with the declaration
  boot: {}           # Architecture, menu timeout, auto-boot timeout
  auth: {}           # Session/certificate/token policy
  registration: {}   # Registration window TTL and admission quota
  credentials: {}    # DHHC-1 rotation and push reconciliation period
  agentTimeoutSec: 10
```

Except for `spec.networking` and `metadata.name`, every block has defaults. The full key table is in the [Configuration Reference](../../reference/configuration.md).

## Network Declaration

`spec.networking` is the single authority for dnsmasq configuration:

- `interface` becomes `interface=`;
- `subnet` determines the netmask;
- `dhcpRange` becomes the address pool;
- `gateway` / `dns` become DHCP options 3/6.

At startup the control plane generates `<state>/control-plane/dnsmasq/dnsmasq.conf` from the declaration. **Generated files must not be edited by hand**, or they are overwritten on the next start. `kurrent config status` reports whether the generated file matches the declaration.

## Policy Blocks

| Block | Runtime behavior constrained |
| --- | --- |
| `pki` | Bootstrap token single use and TTL, component certificate lifetime, renew threshold, CRL re-sign period |
| `serverCert` | Management server certificate SAN and days; drift is reported only by default |
| `boot` | Boot architecture, menu timeout and auto-boot timeout |
| `auth` | Upper bounds for session, user certificate and management token lifetimes |
| `registration` | Registration window TTL and admission quota; the window is never opened automatically |
| `credentials` | DHHC-1 periodic rotation and push reconciliation period |

## Related Pages

- Back to [Declarative Configuration](../configuration.md)
- Node-side declaration: [Node Declaration](./node.md)
- Validation and application: [Validation and Application](./validation.md)
