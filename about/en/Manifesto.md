# Our Definition of Cloud Native

**By LECREATE**

When people see my project, they always say the same thing: "Oh, that diskless-boot thing."

I don't blame them. Diskless booting is the skin. But today I want to write down, in my own words, what cloud native means to me — and to set one thing straight: **Kurrent was never a diskless-boot project.**

## 1. The Last Decade of Cloud Native Was Only Half the Story

When people say "cloud native" today, they mean containers, Kubernetes, microservices, declarative configuration, elastic scaling. All correct, all valuable — these ideas turned **applications** into stateless, schedulable things no longer chained to any particular machine.

But nobody ever asked: who installs the OS on the nodes underneath those Pods? Who drives to the data center to reimage a failed server? Who migrates workloads machine by machine when hardware generations turn over? Kubernetes doesn't answer. It turns away gracefully and treats that layer as someone else's problem.

So the cloud native of the past decade has been **cloud native from the operating system up**. It turned applications into vapor, but the compute beneath them is still blocks of ice — heavy, fixed, bolted inside metal boxes, each failure demanding a human come freeze a new one.

We spent ten years making everything above the cloud flow. Everything except the ground the cloud itself stands on.

**That is not cloud native. That is half of cloud native.**

## 2. My Definition: Turn Compute into Vapor, Top to Bottom, Not a Single Block of Ice

The essence of cloud is not "running VMs in someone else's data center." The essence is one sentence: **compute is not bound to any specific piece of hardware.** It has no fixed shape. It condenses when needed and dissipates when not; where it comes from and where it goes are irrelevant. Water does not remember which cloud it was in a moment ago. Compute should not remember which machine it was on a moment ago, either.

So my definition is almost brutally simple:

**True cloud native means extending statelessness all the way down to the compute layer itself — applications are vapor, and the compute that carries them is vapor too. Vapor all the way down. Not a single block of ice.**

Kubernetes spent ten years making applications into cloud.
**What I am doing is making compute into cloud.**
The day those two layers merge, the phrase "cloud native" will be whole for the first time.

## 3. Diskless Is the Means. Statelessness Is the Soul.

Now let me explain why my project is not a diskless project.

Diskless is a physical form — no local hard drive. But what I have always wanted is not "no disk." It is **statelessness**: a compute node holds no persistent state of its own. Identity, operating system, data — all are provisioned externally by the network and the control plane. Disposable. Replaceable. Rebuildable in an instant.

At its core, diskless boot is about **identity addressing**: the moment a machine powers on, three questions must be answered — *whose identity, which target, which disk.* The entire industry has spent decades hard-wiring those answers into the machine itself — Linux baked into an initramfs, a human performing surgery every time a new node is added. What I do is pull those answers out of the machine and hand them to the network: the firmware hands the boot tables (NBFT/NBCT) over at boot, the control plane injects real identity through a dynamic variable chain, and **not a single byte of machine identity is ever written to the disk.**

**Let identity flow.** When identity flows, disks decouple from machines. When disks decouple from machines, compute becomes a free particle that can move across any substrate. Diskless is merely the outermost appearance of that process.

## 4. Decoupling the Disk from the Machine

The virtualization industry has had a wall for twenty years: when a guest OS inside a VM needs to run on bare metal, you perform a P2P conversion — swap drivers, swap the bootloader, swap the HAL. Windows just blue-screens. The wall exists because **the OS is coupled to the hardware it runs on.**

My answer is to make the OS hardware-agnostic: generic driver injection, a unified iPXE/NVMe-oF boot chain, and a disk that is nothing but a pristine block volume. **The operating system on that disk never knows — and never needs to know — whether it is running inside a VM or on bare metal.**

**A system disk running inside a VM can be moved to bare metal with a single click** — and vice versa. No conversion. No reinstall. No migration. Because there was never anything to migrate: the disk has not moved an inch. Only the hands holding it have changed.

When compute can drift freely between the two sealed worlds of "virtual machine" and "bare metal," what breaks is not a feature. It is an ontological boundary. **From that moment on, diskless is no longer diskless — it is the freedom of compute.**

## 5. One Semantics, Every Layer

This stateless provisioning semantics does not distinguish between bare metal and virtual machines. PVE runs on Debian, so PVE itself can boot disklessly; and inside a disklessly booted PVE, virtual machines can boot disklessly too. Layer nested inside layer, the same paradigm repeating itself.

**This means the project is not a solution for one layer. It is a meta-protocol that spans every compute layer.** Kubernetes unified the container layer. What I am unifying is a larger set: every compute unit that can boot from the network — whether it runs on iron or inside a hypervisor.

This is what true cloud native architecture should look like: self-similar, nestable, with no ceiling between layers. Not one layer of cloud. Cloud at every layer.

## 6. Who Wouldn't Want This? — Eventually, Even the Home Lab Will

I have never believed I am inventing demand.

Who doesn't want a machine to come alive the moment you plug in an Ethernet cable? Who doesn't want disks to drift freely between virtual and physical? Who doesn't want swap-and-go instead of a 3 a.m. trip to the data center? Who doesn't want to migrate hundreds of machines with one click when hardware generations turn over? Who doesn't want infrastructure they can understand, modify, and never be locked into by a vendor?

This is not a technology preference. **This is enterprise instinct** — the same instinct that once made everyone say, "I need Kubernetes." That instinct has hung unmet at the bare-metal layer for twenty years. Nobody caught it. I did.

And it will not stop at enterprise data centers. **Who wouldn't want this? Eventually, even the home lab will.** The NAS in your closet, the soft router, the mini PC running Home Assistant — they carry the same pain as the enterprise floor, just at a smaller scale. When statelessness becomes as natural as air at the enterprise level, it will inevitably spill into every home, and every ordinary person will be able to say about their digital life: *it is mine, it is free, and it is not held hostage by a single hard drive that could fail tomorrow.*

**The subject of cloud native will eventually expand from the enterprise CTO to every individual.** And that is where my road leads.

## 7. "So It Can't Boot Without a Network Cable?"

When people hear about stateless cloud native, they always ask: "So if there's no network cable, it can't boot, right?"

Yes. I say that without hesitation.

But the logic behind that question is like holding up a candle and saying to a lightbulb: "Look — without electricity, you can't shine."

Correct. A lightbulb truly cannot shine without electricity. The first lightbulbs were unstable — flickering, short-lived, nothing like what we have today. But their significance was enormous: for the first time, light was plugged into a grid. The people who came after did not abandon the lightbulb because the first ones flickered. They refined the filament, improved the vacuum, iterated and iterated, until the lightbulb became something nobody thinks twice about.

"Can't boot without a network cable" and "can't shine without electricity" are the same kind of fact: **it is not a flaw; it is a property.** It means compute has been plugged into a grid for the first time — identity, OS, and data are supplied by the network, just as light is supplied by the grid. A candle carries its own flame and is admittedly more "stable," but it will always be a candle, its light forever limited to that one wick.

Nobody today refuses the lightbulb and keeps candles burning year-round just in case the grid goes down. We accept the grid as a premise and build redundancy and resilience on top of it. The network is the same: build high availability, not a retreat to local disks.

One day, stateless cloud native will be like water and electricity — **once unimaginable, then unremarkable.**

## 8. Protocols May Change. The Direction Will Not.

Someone will ask: what if iSCSI becomes obsolete one day?

Then we change the protocol. **iSCSI can be replaced. The direction cannot.**

What I have planted is a direction: identity that flows, disks decoupled from machines, compute unbound from any specific hardware. iSCSI is merely the first-generation vehicle carrying that direction. Yes, there will be new protocols, new implementations — just as the filament in a lightbulb has been replaced many times over, but "getting light from the grid" has never been replaced.

Someday, storage nodes will hit their limits. When that day comes, I may well go study the next generation of storage protocols. That would not be a betrayal of today's work. It would be its continuation — a lightbulb iterates for a century, and no one says the lightbulb was wrong.

The direction is planted. This road is not mine to walk alone into the dark. Whoever recognizes this direction — let us walk it together.

**Let's build this, together.**

## 9. Closing

Someone asks: when will all of this arrive?

I am in no hurry. The road is paved inch by inch, the potholes filled one by one. The rest, I leave to time.

**All truly means All. Ready truly means Ready.**

And the one thing I have been saying, from the very first word to the very last, is this:

**Yes. This is cloud native.**

