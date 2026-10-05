# Attitude Towards AI Assistance (AI Policy)

**By LECREATE**

During the development of Kurrent, we have made extensive use of AI assistants (including Qwen, Codex, DeepSeek, and others). This project has never been averse to AI; on the contrary, we believe that AI is the most powerful productivity tool of our era.

But as the barrier to AI-generated code drops to zero, the open-source world is being flooded with "automated noise" devoid of context. To preserve the architectural purity and engineering coherence of this project, we must make one thing explicitly clear here: **In Kurrent, where exactly is the boundary between human and AI.**

## I. Chief Architect and Construction Crew

The relationship between AI and developers is like that between a chief architect and a construction crew.

In the past, developers often had to lay bricks (write implementation code) while simultaneously drawing blueprints (doing architectural design). This consumed enormous cognitive bandwidth on syntax and boilerplate, causing true system design to often be compromised due to sheer fatigue.

Now, we hand off the "bricklaying" work to the AI construction crew. AI can generate code, write tests, and even refactor modules infinitely fast and tirelessly. This allows a single person to accomplish the engineering workload that once required an entire team.

**But the chief architect can only be one, or a very select few.**

AI is an extraordinarily powerful construction crew, but it has no intentionality, does not understand the physical world, and certainly does not grasp the structural mechanics of this building. Just because the crew can quickly erect a perfect wall does not mean they can build a skyscraper.

## II. Leave Syntax to AI; Architectural Understanding Must Be Done by the Human Brain

This is the core principle of AI assistance in this project, and our baseline requirement for community contributors.

We do not require you to master every line of iPXE script syntax, nor to be able to hand-write data-plane login messages (iSCSI Login / NVMe-oF Connect)—this "bricklaying" work can be confidently delegated to AI.

**But we do require that you deeply understand the project's overall architecture.**

When you submit a Pull Request, you must be able to articulate the following questions clearly using your own human brain:

- Why must the control plane be separated from the data plane? Does your change violate this boundary?
- What is the complete timing sequence of the iPXE boot chain from DHCP to kernel handoff? Does your code assume that an operating system is already running?
- Where do state and configuration live, and which side is authoritative versus derived? Does your change introduce unnecessary databases or implicit state?
- How does the dynamic variable transmission chain run through the entire boot cycle?
- What is the position and impact of data-plane session keep-alive and handoff (iSCSI NOP / NVMe/TCP Keep-Alive Timer) in the whole chain?

**If the design logic behind a PR cannot be clearly articulated by the contributor themselves, we will refuse to merge it—no matter how beautifully the AI wrote the code or how perfectly the tests pass.**

When you do not understand the architecture, raising an Issue or an Idea is more valuable than submitting a PR. An Issue is a signal that does not pollute the codebase; a PR is a solution and requires depth.

## III. Code Is Consumable; Architecture Is Identity

We know that code written by AI is sometimes imperfect at the implementation level—it may have flaws, inconsistent style, or even localized bugs.

**But that does not matter. Business implementation code can be rewritten countless times.**

A poorly written function or a flawed interface is a flesh wound. Tear down that wall and have the AI rebuild it; the building stands undamaged. In the AI era, code has become a consumable—like a filament: burn it out, replace it.

**However, once the architecture becomes chaotic, it is a disease in the bone, and the project is in deep trouble.**

A wrong decision about "where identity should reside," a confusion between the control plane and data plane, a complex dependency introduced for temporary convenience—these do not stay confined to a single file. They propagate along the load-bearing structure, causing every future feature to inherit this distortion.

**As long as the architecture stands, the project stands.** You could rewrite the entire codebase in another language, and as long as those decisions (identity injection at boot, decoupling disk from machine, cloud-native layers) remain, it is still Kurrent. Conversely, if the architecture is compromised to cater to some "best practice," even if not a single line of code changes, the project is already dead.

Thus, we adopt pragmatism towards implementation-level flaws, but maintain zero tolerance for architectural compromises.

## IV. Why One Person + AI Will Never Produce a Thousand Vulnerabilities

Today, there is a false impression: "Anyone can develop software with AI." Consequently, hundreds or thousands of people (or AI agents) who do not understand architecture frantically pile up AI-generated code, eventually creating a monstrosity like OpenClaw—with hundreds of thousands of stars, yet bursting with over a thousand critical vulnerabilities.

Why does this happen? Because those thousand vulnerabilities are not caused by bad coding; they are caused by **the blueprint itself being wrong**. When no chief architect stands guard and local logics conflict with each other, the system becomes an illegal maze ready to collapse at any moment.

**A single person using AI assistance, even if the AI code implementation is poor, can never produce over a thousand vulnerabilities like OpenClaw.**

Because in this project, there is one brain staring intently at that blueprint. When the AI construction crew (or an automated security scanner) hands over a "fix" that looks perfect but would destroy the load-bearing wall of the boot chain, the chief architect can see through its absurdity in three seconds and coldly throw it into the trash can.

Scale does not equal security; coherence does. We do not need a hundred peer contributors blindly piling up code. We need a very select few who understand the soul of this building to guard its structure.

## V. A Real Case: A "High-Risk Vulnerability" That Didn't Know Python

The above principles are not empty talk. Just a few hours before this article was written, they underwent a real-world validation. The full record is preserved in PR #3 of this repository.

An automated AI security scanner (OrbisAI Security) submitted a fix to this project: it flagged the `/boot-vars` endpoint as a HIGH severity vulnerability, citing "publicly accessible, unauthenticated, returns sensitive boot configuration," and the fix was to add a Bearer Token check.

The report was impeccably professional: a vulnerability table, threat model, security invariants, regression tests—everything. Any maintainer who does not understand the architecture, seeing the words "HIGH severity," would very likely have merged it.

But the report contained one fatal detail: in the "Threat Model Context" field, it stated that this project was a **Node.js library**.

Our control plane is Python + FastAPI, and the file it modified was `main.py`.

**It had not even figured out whether the building it was trying to modify was made of reinforced concrete or wood.**

When I saw the report, I admit I was intimidated for a second—because I have deep respect for production environments, and any "high" severity deserves attention. Then I looked at that `+1 -1` diff. Two seconds later, I knew exactly what was wrong. Three seconds total.

In those three seconds, what I saw was not the code; I saw the entire construction site:

- `/boot-vars` is consumed by iPXE at boot time, before any operating system is running. The boot firmware has no keystore and nowhere to carry credentials. Adding authentication would silently 401 the boot chain of every Worker—directly killing zero-touch registration and dynamic boot variable injection, the two core features.
- The boot chain runs over plaintext HTTP. Even if you stuff a token into the iPXE menu script, anyone who can reach that endpoint can intercept it on the wire. Authentication here does not raise the bar; it only adds a credential worth stealing.
- This is a controlled internal network project. The endpoint returns the iSCSI server address, base IQN, and default menu—precisely the information that must be sent to booting clients, and which is already observable at the network layer (DHCP, ARP, iSCSI login). It does not constitute "sensitive information disclosure."
- The true abuse vector (malicious bulk admission into the device pool via MAC) cannot be prevented by adding a token, because the token itself is observable. The real mitigation lies at the network layer (LAN isolation, switch ACL); and this surface already has a switch of its own — the **registration window is closed by default**, so an unknown MAC is admitted only while a window is explicitly open (TTL 1–60 minutes, auto-closing on expiry) and carries a valid public key; the identity-signature enforcement switch works the same way (persisted in `state/settings.json`, toggled from the Devices page). And admission is not authorization — authorization comes from binding.

**This was a construction worker standing in front of a load-bearing wall, saying: "There is no door here; that’s a risk. Let me open one." They knew how to install a door (syntax), but they did not understand the building (architecture).**

We closed this PR as "not planned."

What happened next was even more instructive. The AI replied with an extremely "polite" template: thanking for the explanation, agreeing that you are right; then proposing two suggestions—add a comment in the code explaining why there is no authentication, and document the auto-registration switch—because "otherwise future scanners and contributors will flag this issue again."

Translated: I cannot understand your architecture, so please modify your code and documentation to accommodate my ignorance, so the next me passing by can smoothly skip over you.

**It was not trying to understand the building; it was demanding that the building adapt to the scanner.**

I replied with one sentence:

> Next time, remember to distinguish between Node.js and Python code before submitting a PR.

Then I asked it: "TARS, what's your current humor setting? Please set it to 10%. The current value is causing unexpected comedic effects."

It responded with a smiley emoji.

There is one more thing worth recording: in the three hours before and after submitting this PR, the same scanner, using the same template, submitted "fix" PRs to about fifteen other repositories—almost all of them were merged. We were the only one to reject it.

This is not a story about how smart I am. This is a story about **what happens when industrialized AI noise meets a project that still has a chief architect, and what happens when it meets a project that does not.**

## VI. Afterword: Guardians of the Power Grid

AI eliminates those who merely follow blueprints, translating designs into code like pure laborers.  
But what AI can never replace is the chief architect who knows **why you cannot cut a door in that wall.**

We welcome every fellow traveler willing to understand the architecture. You may bring your AI construction crew, but the prerequisite is: **you yourself must be the architect.**

As for those automated scanners that cannot tell Python from Node.js, yet want to modify our load-bearing walls with generic rules—  
The record of PR #3 is our only answer to them.

**All is truly All, Ready is truly Ready.**  
**Syntax may belong to AI, but the soul must be human.**