---
layout: home

hero:
  name: Kurrent
  text: Make bare metal flow
  tagline: Cloud-native stateless bare-metal delivery
  actions:
    - theme: brand
      text: Deployment Guide
      link: /en/guide/deployment
    - theme: alt
      text: GitHub
      link: https://github.com/dutyc/kurrent
---

> The Way flows through the six empty lines, above and below without constancy; the firm and the yielding interchange — it adapts to whatever change may come. — the I Ching · Great Commentary (Xici), Part II

Kurrent (周流, zhōuliú — "circling flow") is a cloud-native, stateless bare-metal delivery platform: as Kubernetes makes applications cloud-native, Kurrent makes compute cloud-native — compute is no longer bound to hardware; like an electric current, it flows across bare-metal servers and takes whatever form the moment demands.

## Features

- **Stateless compute** — compute nodes hold zero persistent state; plug in and boot.
- **Plug-and-play** — auto-joins the pool at boot; delivered from the Web UI in one click.
- **Zero learning curve** — declarative configuration and a CLI that follow kubeadm / kubectl command semantics.
- **Dual data plane** — NVMe-oF and iSCSI; storage scales out horizontally.

## Documentation

- [Deployment Guide](./guide/deployment)
- [Manifesto](https://github.com/dutyc/kurrent/blob/main/about/en/Manifesto.md)
