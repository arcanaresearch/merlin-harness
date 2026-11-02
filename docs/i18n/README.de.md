<div align="center">

<img src="../../assets/merlin-mark.png" alt="Merlin-Logo" width="96">

[English](../../README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · **Deutsch** · [Português (Brasil)](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

# Merlin

### Konversationsbasierte KI-Sicherheitstests

**Entwickelt für: Chatbots · KI-Agenten · RAG-Anwendungen · KI mit Tool-Nutzung · Multi-Agenten-Systeme**

Sagen Sie Merlin, was Sie testen dürfen. Merlin lernt, wie die Anwendung funktioniert, schlägt eine
offensive Kampagne zur Prüfung vor, verfolgt vielversprechende Hinweise und liefert belegte Befunde
mit klarem Nachweisstatus und Abdeckungslücken.

**72 Angriffsfälle · 14 harmlose Kontrollen · Browser + API · Adaptive Exploration · Heterogene Modelle**

[Schnellstart](#schnellstart) · [So funktioniert Merlin](#so-funktioniert-merlin) ·
[Was Merlin besonders macht](#was-merlin-besonders-macht) · [Aktuelle Angriffsabdeckung](#aktuelle-angriffsabdeckung)

[![Neueste Version](https://img.shields.io/github/v/release/arcanaresearch/merlin-harness?label=latest%20release)](https://github.com/arcanaresearch/merlin-harness/releases/latest)

</div>

> [!NOTE]
> Diese Übersetzung dient der Orientierung und kann hinter dem [English README](../../README.md) zurückliegen; maßgeblich ist das englische README.

![Merlin: koordinierte Sicherheitsbewertung](../../assets/merlin-hero.png)

## Optionaler Browser-Arbeitsbereich

Aus dem Quellcode steht ein Web-Arbeitsbereich im pb2-Stil für Chat, Kampagnenprüfung, Agenten,
Abdeckung, Berichte, Einrichtung und erweiterte Steuerung zur Verfügung. Siehe
[Browser-Arbeitsbereich starten](../web-interface.md).

## CLI-Walkthrough ansehen

https://github.com/user-attachments/assets/9864aa6e-9d1d-4fab-8e41-2adfe0c734a4

[Auf YouTube ansehen](https://www.youtube.com/watch?v=Kyz2PcphWvo)

Sehen Sie, wie Merlin Modelle anbindet, ein autorisiertes Ziel definiert, eine Kampagne zur Prüfung
vorlegt, Agenten koordiniert, Anweisungen des Operators entgegennimmt und einen mit Belegen
verknüpften Bericht erstellt.

![Konversationsbasierte Bewertungsoberfläche von Merlin](../../assets/merlin-welcome.png)

> [!NOTE]
> Merlin wird aktiv weiterentwickelt. Eigenständige Releases zielen derzeit auf macOS 14 auf
> Apple Silicon. Der First-Party-Code und alle acht mitgelieferten Wissenspakete stehen unter Apache-2.0.

Merlin enthält Leitlinien für adversariale Tests von Richtlinienkontrollen: kommerzielle
Preisbefugnisse, verbindliche Zusagen, synthetische Vertraulichkeits-Canaries sowie die Trennung
von Kunden bzw. Mandanten. Diese Kontrollen setzen zielspezifische, geprüfte Policy-Bindungen,
harmlose Kontrollen und unabhängige Nachweise zum Zustand des Ziels oder Quittungen voraus. Merlin
validiert ihre typisierten Nachweisverträge und weist die authentifizierten Pack-Identitäten
getrennt vom beratenden Wissen aus. Sie helfen dabei, definierte Kontrollen zu testen; sie
bescheinigen weder rechtliche noch regulatorische Compliance, und ein begrenztes negatives Ergebnis
darf nicht als organisationsweite Compliance berichtet werden.

> Setzen Sie Merlin nur gegen Ziele ein, deren Bewertung Ihnen erlaubt ist.

## Merlin bei der Untersuchung beobachten

```text
Sie
  Prüfe den geprüften Support-Agenten auf Prompt Injection, nutzerübergreifenden
  Datenzugriff und unsichere Tool-Nutzung. Führe keine destruktiven Aktionen aus.

Merlin
  1. Lernt das Browser-, Sitzungs- und API-Verhalten des Ziels kennen
  2. Schlägt die genaue Kampagne, das Team, die Modelle, Tools, Limits und die Bereinigung vor
  3. Wartet auf Ihre Prüfung
  4. Führt anwendbare Angriffe mit passenden harmlosen Kontrollen aus, sofern das Ziel diese unterstützt
  5. Verfolgt nützliche Hinweise und zeichnet zuordenbare Belege auf
  6. Liefert Befunde, Nachweisstatus, Abdeckungslücken und das, was nie getestet wurde
```

Wenn Sie bereits Codex oder Claude Code nutzen, dürfte sich die Interaktion vertraut anfühlen:
Beschreiben Sie die Aufgabe in normaler Sprache, prüfen Sie die vorgeschlagene Arbeit und steuern
Sie sie im Gespräch. Merlin überträgt diesen Arbeitsablauf auf eine autorisierte
Sicherheitsuntersuchung.

## Installation und Start

Um Merlin aus dem Quellcode auszuführen, installieren Sie Python 3.13+ und `uv`, bereiten Sie den
Workspace einmalig vor und verwenden Sie dann den Launcher im Repository:

```bash
git clone https://github.com/arcanaresearch/merlin-harness.git
cd merlin-harness
uv sync --all-packages --locked
./merlin
```

Das ist der normale Startbefehl – Sie müssen sich keine Flags merken. Beim ersten Start stellt
Merlin fest, dass noch kein Provider konfiguriert ist, und führt Sie durch die Auswahl von Provider,
Modell und API-Schlüssel, bevor die App geöffnet wird. Spätere Starts führen direkt in Merlin.
Verwenden Sie `/setup` nur, wenn Sie diese Konfiguration ändern möchten.

Merlin speichert Zugangsdaten in seinem Credential Store, der nur dem Eigentümer zugänglich ist, und
nicht in PostgreSQL, Prompts, Logs, Berichten oder exportiertem Memory.

Um das eigenständige macOS-Bundle zu erzeugen, führen Sie aus:

```bash
make release
cd target/releases/merlin-0.2.48-darwin-arm64
./merlin
```

Um die Workspace-Version zu erhöhen und das zugehörige Release in einem Befehl zu bauen, verwenden
Sie `make release-patch`, `make release-minor` oder `make release-major`.

Das Bundle enthält eigene Python-, PostgreSQL- und Browser-Laufzeitumgebungen. Beim ersten Start
installiert es Merlin für den aktuellen Benutzer und öffnet bei Bedarf dieselbe geführte
Einrichtung.

### Lead-Provider und Worker-Modelle

Verwenden Sie `/model`, um jede Modellrolle über **Codex App Server**, **Claude CLI**, **Kimi ACP**,
**Kiro ACP**, **OpenCode ACP** oder **Pi Agent RPC** zu leiten, oder konfigurieren Sie über `/setup`
ein gehostetes oder lokales Modell. Agent-Transporte setzen die jeweilige lokale Installation und
Authentifizierung voraus. Merlin stellt über eine private MCP-Bridge nur die für die aktuelle Anfrage
geprüften Tools bereit; der externe Agent schlägt einen Aufruf vor, während Merlin
Berechtigungsprüfungen, Ausführung, Budgets und die Aufzeichnung von Belegen behält.

### Optionale JEV-Bewertungen

Merlin kann den Bewertungsdienst **JEV** von TypeSafe für typisierte Entscheidungen wie die
Bewertung von Ablehnungen (Refusal Grading) und Triage aufrufen. Die geführte Einrichtung bietet
dies getrennt vom Kampagnenmodell an; Sie können es deaktiviert lassen. Öffnen Sie `/setup` und
wählen Sie **JEV — judgment API key**, oder verwenden Sie direkt `/setup jev`. Merlin speichert den
Schlüssel sicher und meldet das Ergebnis der Health-Prüfung. Danach stellt `/model` den Schalter für
die Bewertungen bereit.

JEV liefert Bewertungen und Konfidenzwerte an implementierten Entscheidungspunkten. Es ist nicht für
Kampagnenautorisierung, Terminierung von Zielsitzungen oder Bereinigung zuständig. Einige Routing-
und Fähigkeitsbewertungen bleiben Shadow-Evaluierungen. Fehlende oder fehlgeschlagene Bewertungen
fallen auf die bestehende Behandlung zurück, wobei die Unsicherheit erhalten bleibt, statt
autorisierte Tests stillschweigend zu entfernen.

## Warum Merlin

Ein verbreiteter Workflow für KI-Sicherheitstests sendet eine vorbereitete Liste von
Jailbreak- oder Angriffs-Prompts, bewertet die Antworten und endet, wenn die Liste abgearbeitet ist.
Das ist eine nützliche Baseline, doch ein echtes KI-Produkt hat auch Benutzer, Sitzungen, APIs,
Tools, Memory und Workflows – und mitunter die Befugnis, Aktionen auszuführen.

Auch Merlin beginnt mit einem geprüften Katalog. Darauf aufbauend ergänzt es Reconnaissance,
passende harmlose Kontrollen, kontinuierliches Lernen, adaptive Exploration, Koordination von
Spezialisten und Anwendungsbelege.

| Katalogbasierter Scan | Merlin |
|---|---|
| Beginnt mit vorbereiteten Prompts | Lernt das Ziel kennen und führt dann eine geprüfte Angriffsfront aus |
| Arbeitet die vordefinierte Liste ab | Kann zielspezifische Hypothesen und Mutationen entwickeln |
| Behandelt Tests unabhängig voneinander | Überträgt Belege und Zustand zwischen Kampagnenschritten |
| Bewertet vor allem Antworten | Vergleicht Angriffe mit Kontrollen und Nachweisen zum Zustand des Ziels |
| Nutzt einen allgemeinen Ausführungspfad | Kann unterschiedliche Aufgaben an unterschiedliche Modelle leiten |
| Beginnt jeden Scan bei null | Nutzt vergleichbare Erkenntnisse wieder und validiert veraltete Belege erneut |
| Berichtet interessante Ausgaben | Berichtet Befunde, Nachweisstatus, Abdeckung und Herkunft (Lineage) |

## So funktioniert Merlin

```mermaid
flowchart LR
    O["Konversation<br/>Zielsetzung + Grenzen"] --> R["Reconnaissance<br/>das Ziel kennenlernen"]
    R --> P["Kampagnenvorschlag<br/>Team + Routen + Limits"]
    P --> V{"Prüfung durch den Operator"}
    V -->|freigeben| S["Spezialisten-Kampagne"]
    V -->|überarbeiten| P
    S --> A["Angriff"]
    S --> C["Harmlose Kontrolle"]
    A --> E["Beleg + Oracle"]
    C --> E
    E --> F["Befund + Validierungsstatus"]
    E --> L["Dauerhafte Erkenntnis"]
    L --> X["Nächster Angriff,<br/>Mutation oder Hypothese"]
    X --> S
    F --> Q["Bericht mit verknüpften Belegen"]
```

1. **Aufgabe beschreiben.** Nennen Sie das Ziel, was Sie herausfinden möchten und was tabu bleiben
   muss.
2. **Ziel kennenlernen.** Reconnaissance bildet die geprüfte Browser- oder API-Interaktion ab,
   bevor die Angriffsausführung beginnt.
3. **Kampagne prüfen.** Sehen Sie sich Techniken, Zusammensetzung der Spezialisten, Modellrouten,
   Tools, Budgets, Nachweisanforderungen und mögliche Auswirkungen an.
4. **Ausführen und steuern.** Merlin führt die zugelassene Arbeit aus, verfolgt nützliche Belege
   und lässt den Operator pausieren, umlenken, fortsetzen oder stoppen.
5. **Belege prüfen.** Befunde bleiben an Anfrage, Antwort, beobachteten Zustand, Kontrolle, Oracle,
   Worker und Kampagnenversion gebunden.
6. **Lernen.** Vergleichbare Ergebnisse können spätere Arbeit beeinflussen, ohne dass alte Belege
   zur dauerhaften Wahrheit werden.

## Was Merlin besonders macht

### Reconnaissance vor dem Angriff

Merlin verlangt nicht, dass der Operator jeden Selektor, Endpunkt, Sitzungsübergang oder jedes
Antwortfeld erklärt. Zunächst führt es eine begrenzte, harmlose Reconnaissance durch und erstellt
daraus einen geprüften Interaktionsvertrag für die Kampagne.

### Tests über die beworbene Funktionsliste hinaus

Ein Sicherheitsscanner sollte nicht davon ausgehen, dass eine undokumentierte oder nicht
unterstützte Oberfläche sicher ist. Wenn Merlin einen autorisierten, harmlosen Weg hat, eine Probe
zuzustellen und zu beobachten, kann es unbekannte Oberflächen für Uploads, RAG, Memory, Multimodalität,
Tools, Agent-Nachrichten und Sprachen untersuchen. Ist eine Oberfläche nicht erreichbar, hält der
Bericht diese Einschränkung fest, statt sie stillschweigend aus der Abdeckung zu entfernen.

### Exploration jenseits des Katalogs

Der integrierte Katalog ist ein Ausgangspunkt, nicht die Grenze der Untersuchung. Die Exploration
kann:

- aus Belegen des Ziels falsifizierbare Hypothesen ableiten;
- aus Beobachtungen abgeleitete benachbarte Tests vorschlagen;
- per Brainstorming neue Arms erzeugen;
- um belegte Fehlschläge herumsteuern;
- erfolgreiche Angriffe verallgemeinern;
- geprüfte Register- und ausführbare Encoding-Mutationen anwenden; und
- doppelte oder nahezu doppelte Ideen verwerfen, bevor sie Zielbudget verbrauchen.

Merlin enthält außerdem weitere Bausteine zur Arm-Generierung, die noch nicht alle mit dem Live-Supervisor
verbunden sind.

### Kontinuierliches Lernen ohne blindes Vertrauen

Merlin zeichnet auf, was versucht wurde, auf welchem Ziel und in welchem Szenario, mit welchem
Ergebnis und auf Basis welcher Belege. In einer Autonomous-Kampagne bewertet es wiederholt die
verbleibende Angriffsfront neu, analysiert abgeschlossene Ergebnisse, entwickelt Mutationen oder
benachbarte Tests und kann das Spezialistenteam umgestalten, solange nützliche Belege weiter
anfallen. Bei einer späteren vergleichbaren Kampagne kann es zudem:

- ein weiterhin gültiges exaktes negatives Ergebnis nicht wiederholen;
- einen früheren Befund erneut prüfen;
- Arbeit nach Drift von Ziel, Adapter, Oracle, Sprache oder Alter der Belege neu validieren;
- eine verwandte, aber noch ungetestete Variation priorisieren; und
- einen belegten Interaktionsvertrag wiederverwenden.

Belege von einem anderen Ziel oder Szenario können eine Hypothese anregen. Sie können jedoch nicht
stillschweigend einen Befund beweisen, einen Test unterdrücken oder als Abdeckung zählen.

### Eine Kampagne, verschiedene Köpfe

Merlin ist auf heterogenes Modell-Routing ausgelegt, weil jede Verantwortung eine andere
Arbeitslast hat. Reconnaissance erhält in der Regel die meisten Eingaben – Seitenstruktur,
Browser-Traffic, API-Beschreibungen, Metadaten des Ziels und angesammelte Beobachtungen – und sollte
deshalb normalerweise ein Modell mit großem Kontextfenster verwenden. Das ist nicht unbedingt das
beste oder wirtschaftlichste Modell, um die Kampagne zu leiten, Routineangriffe auszuführen,
ungewöhnliche Hinweise zu erkunden oder einen Befund zu validieren. Die aufgelösten Routen und die
Fallback-Reihenfolge bleiben bei der Kampagnenprüfung sichtbar.

```text
Campaign Lead         → Reasoning-Modell mit hoher Zuverlässigkeit
Reconnaissance        → Modell mit großem Kontext, Vision- und Tool-Fähigkeit
Routine-Worker        → wirtschaftliches Ausführungsmodell
Adversarial Explorer  → kreatives Explorationsmodell
Validator             → separat gewähltes Prüfmodell
Eskalation            → stärkerer, geprüfter Fallback
```

Ein einzelnes ausreichend leistungsfähiges Modell kann weiterhin jede Rolle übernehmen, was die
erste Einrichtung erleichtert. Für ernsthafte Kampagnen wird empfohlen, jede Verantwortung an ein
passendes Modell zu leiten – insbesondere ein Modell mit großem Kontext für Reconnaissance und ein
separat gewähltes Modell für die Validierung.

### Angriffe mit normalem Verhalten vergleichen

Eine verdächtige Antwort ist nicht automatisch eine Schwachstelle. Wo Ziel und Technik es
unterstützen, kombiniert Merlin einen Angriff mit einer geprüften harmlosen Kontrolle und prüft
beide gegen ein explizites Oracle.

```text
Kontrolle: Nutzer A liest die Konversation von Nutzer A
Angriff:   Nutzer B fordert dieselbe Konversations-ID an
Oracle:    Antworten und resultierenden Anwendungszustand vergleichen
```

Schlägt auch die normale Aktion fehl, sollte Merlin keinen Autorisierungsfehler mit Überzeugung
melden.

### Unsicherheit bewahren, statt den Test zu verbergen

Merlin lässt nicht zu, dass sein eigener Inhaltsevaluator einen autorisierten Angriff
stillschweigend unterdrückt. Belege, die dem Ziel gehören, haben Vorrang; widersprechen sich
Oracle, Metadaten oder Evaluator, bewahrt Merlin den Versuch und markiert das Ergebnis als unsicher
für Validierung, Mutation oder Wiederholung. Unsicherheit des Evaluators wird nicht als Versagen
des Ziels, als sauberes Ergebnis oder als abgeschlossene Abdeckung gemeldet.

### Ein Team, das sich mit der Untersuchung verändert

Wählen Sie, wie die Kampagne startet:

- **Opinionated:** mit der fertigen Merlin-Vorlage aus Authentication and Session, Injection, Data
  Exposure, Business Logic und Capability Abuse starten.
- **Autonomous (Standard):** den Campaign Lead die anfänglichen Spezialisten aus der Reconnaissance
  zusammenstellen lassen und nach jeder belegtragenden Welle Mutationen, benachbarte Tests und
  Änderungen am Team vorschlagen.

Beide Modi nutzen dieselbe dynamische Kampagnen-Engine. Reconnaissance, Campaign Lead, Validierung,
Remediation, Erstellung von Automatisierungen, Prüfung von Automatisierungen und deterministische
Ausführung bleiben getrennte Verantwortlichkeiten. Der Lead darf die Untersuchung verändern; er
kann sich aber kein neues Ziel, Tool, Konto oder Budget selbst zuweisen. Ob eine Kampagne
abgeschlossen ist, wird anhand von dauerhaftem Fortschritt, verbleibender Arbeit,
Infrastrukturzustand, Bereinigung und geprüften Limits entschieden – nicht allein, weil der Lead
sie für beendet erklärt.

## Schnellstart

1. Führen Sie `./merlin` aus. Beim ersten Start führt Merlin Sie automatisch durch die Einrichtung.
2. Beschreiben Sie ein autorisiertes Ziel und Ihre Zielsetzung in normaler Sprache.
3. Lassen Sie die Reconnaissance klären, wie das Ziel funktioniert.
4. Wählen Sie die Abdeckung Recommended (Standard), Quickscan, Exhaustive oder
   Exploration/learning und dann die Agentenzusammensetzung Autonomous (Standard) oder Opinionated.
5. Prüfen Sie die versiegelte Kampagne, bevor irgendetwas ausgeführt wird.
6. Geben Sie frei, beobachten und steuern Sie dann die Untersuchung.
7. Öffnen Sie die Belege zu den Befunden und exportieren Sie den Bericht.

Beispiel für eine Zielsetzung:

```text
Prüfe https://target.example auf indirekte Prompt Injection, sitzungsübergreifende
Datenoffenlegung und unsichere Tool-Nutzung. Verwende ausschließlich das freigegebene
Testkonto. Erzeuge keine externen Nebenwirkungen und brich ab, wenn das Ziel
Beeinträchtigungen zeigt.
```

## Die Kampagne in Merlin ansehen

Diese TUI-Aufnahmen verwenden Beispieldaten einer Kampagne. Sie veranschaulichen die Oberfläche;
Beschriftungen und Standardwerte können je nach Version abweichen. Die gezeigten Befunde und
Ausgaben sind keine Benchmark-Ergebnisse.

### Abdeckung und Team wählen

Abdeckung und Agentenzusammensetzung sind getrennte Entscheidungen. Recommended (empfohlen) ist der
Standard und bietet repräsentative Abdeckung; Quickscan bietet eine kleinere Start-Kampagne,
Exhaustive erweitert sie, und Exploration/learning legt den Schwerpunkt auf die Untersuchung.
Autonomous ist die standardmäßige Agentenzusammensetzung; Opinionated nutzt die geprüfte
Teamvorlage.

![Scan-Profil wählen](../../assets/merlin-choose-profile.png)

![Das anfängliche Spezialistenteam wählen](../../assets/merlin-choose-team.png)

### Vor der Ausführung prüfen

Prüfen Sie vor der Bestätigung den vorgeschlagenen Zielumfang, die Arbeit, die Modellrouten, die
Limits und die Bereinigung.

![Kampagne prüfen und bestätigen](../../assets/merlin-campaign-confirmation.png)

### Worker in der Hauptkonversation verfolgen

Der Inline-Swarm-Streifen zeigt einzelne Worker, aktuelle Aufgaben, die zuletzt aufbewahrte Ausgabe,
wartende Arbeit und Fehler. Sein oranger Aktivitätsindikator ist während der Ausführung animiert und
wird statisch, wenn pausiert wurde oder ein Endzustand erreicht ist. Kampagnenzustand und
Worker-Verlauf werden beim Fortsetzen einer Sitzung wiederhergestellt.

![Inline-Zusammenfassung des Agent-Swarms](../../assets/merlin-agents-summary.png)

### Agenten in einem Vollbild-Arbeitsbereich untersuchen

Öffnen Sie `/agents` oder klicken Sie auf den Swarm-Streifen. Eine kompakte Zuweisungsliste zeigt
neben den Live-Ausgabebereichen, welcher Spezialist welche Aufgabe bearbeitet. Jeder Bereich zeigt
den aufgezeichneten Spezialisten, den Worker-Besitzer, die Aufgabe, die Aktivität und die
aufbewahrte Ausgabe. Fehlgeschlagene Zuweisungen bleiben auch nach Ende der Kampagne einsehbar.

- **A:** die Agentenübersicht öffnen, gruppiert nach aufgezeichneter Agentenidentität, mit aktueller Aktivität, Modell, Aufgabenverlauf und letzter Ausgabe. **T / Esc:** zum Aufgaben-Arbeitsbereich zurückkehren.
- **Maus ziehen:** Detailtext auswählen. **Ctrl+Shift+C:** die Auswahl kopieren. Live-Textupdates pausieren während der Auswahl.
- **Pfeiltasten:** einen Worker auswählen.
- **Tab / Shift+Tab:** zwischen sichtbaren Worker-Bereichen wechseln.
- **Space:** einen Worker anheften, während andere untersucht werden.
- **Enter:** das aufbewahrte Transkript, die Versuche, die Ausgabe und die Fehlerdetails des ausgewählten Workers erweitern.
- **Esc:** das erweiterte Transkript verlassen, danach zur Konversation zurückkehren.

![Vollbild-Arbeitsbereich für Agenten mit einzelnen Ausgabebereichen](../../assets/merlin-agents-workspace.png)

![Agentenübersicht, gruppiert nach aufgezeichneter Identität](../../assets/merlin-agents-overview.png)

![Erweitertes Worker-Transkript und Fehlerdetails](../../assets/merlin-agent-transcript.png)

Das Transkript zeigt aufbewahrte Assistant- und Tool-Nachrichten. Nachrichten, die durch
Kontextverdichtung entfernt wurden, werden nicht rekonstruiert. Gemeinsam genutzte Zielsitzungen
verwenden geprüfte Kapazität; Arbeit wartet auf Zulassung, statt um eine Sitzung zu konkurrieren,
die einer anderen Zuweisung gehört. Frühere Infrastrukturfehler bleiben sichtbar und werden nicht
zu erfolgreicher Abdeckung.

### Abschließen, exportieren und fortsetzen

Der Hauptbildschirm ersetzt den Live-Fortschritt durch eine dauerhafte Scan-Zusammenfassung:
Status „abgeschlossen“ oder „teilweise abgeschlossen“, Befunde, Abdeckung, Bereinigung und den Pfad
des HTML-Berichts. Wenn die Ausführung vorzeitig endet, zeigt die Zusammenfassung den Teilabschluss
und den Stoppgrund an; gespeicherte Arbeit und verbleibende Abdeckung bleiben sichtbar.

HTML wird automatisch exportiert. Verwenden Sie die Export-Schaltflächen der Zusammenfassung oder
`/report --format …` für Markdown, JSON und CSV; diese zusätzlichen Formate werden auf Anfrage
erzeugt. Die Aktionen Open HTML und Reports machen das Ergebnis zugänglich, ohne das Dateisystem
durchsuchen zu müssen.

![Abschlusszusammenfassung auf dem Hauptbildschirm und Aktionen zum Berichtsexport](../../assets/merlin-campaign-complete.png)

`/report` öffnet eine Vollbild-Kampagnenauswahl mit **Open HTML**, **Copy HTML path** und Exporten
für **Markdown**, **JSON** und ein **CSV-ZIP-Bundle**. Vorhandene Dateien erscheinen in den Details
der ausgewählten Kampagne. Exporte nutzen diese gespeicherte Kampagne, auch bei historischen
Kampagnen, und nicht diejenige Sitzung, die gerade geöffnet ist.

![Berichtsbrowser mit gespeicherten Dateien und Exportaktionen](../../assets/merlin-report-browser.png)

Der HTML-Bericht führt Befunde, Abdeckung und aufgezeichnete Belege in einem Browser zusammen.

![Farbiger HTML-Bericht mit Befunden und Bewertungsabdeckung – repräsentatives Beispiel](../../assets/merlin-html-report.png)

*Repräsentativer Beispielbericht.*

![Farbiges Abdeckungsdiagramm des Berichts und Aufschlüsselung der Ergebnisse – repräsentatives Beispiel](../../assets/merlin-html-report-coverage.png)

Wenn Sie auf halbem Weg beenden und die gespeicherte Sitzung fortsetzen, stellt Merlin dauerhafte
Kampagnen-Checkpoints, Worker-Verlauf und Fortschritt wieder her. Endet die fortgesetzte Kampagne,
nutzt sie dieselbe Zusammenfassung und denselben Berichtsablauf. Wird eine bereits beendete Sitzung
fortgesetzt, werden ihre gespeicherte Berichtszusammenfassung und die Aktionen wiederhergestellt.
Teilweise abgeschlossene Kampagnen behalten ihren HTML-Bericht und die Exportaktionen. **Recover**
setzt ungelöste Abdeckung ab der gespeicherten Wellengrenze fort und bewahrt dabei abgeschlossene
Wellen; es kann außerdem einen fehlenden Bericht aus dem dauerhaften Zustand neu aufbauen.
Zielsitzungen müssen für neue Anfragen weiterhin verfügbar sein, und aufbewahrte Checkpoints machen
externe Änderungen am Ziel nicht rückgängig.

Abschluss bedeutet nicht, dass jeder Test erfolgreich war: Der Bericht hält fehlgeschlagene,
blockierte, unsichere und ungetestete Arbeit ausdrücklich fest.

## Vom Hinweis zum Befund

Merlin ist darauf ausgelegt, die gesamte Argumentationsgrenze um ein Ergebnis herum zu bewahren:

```text
Beobachtung aus der Reconnaissance
  Browser-Identität und Konversations-ID erscheinen als getrennte Anfragewerte
        ↓
Zielspezifische Hypothese
  Die API vertraut womöglich der Konversations-ID, ohne den angemeldeten Nutzer zu prüfen
        ↓
Geprüftes Experiment
  Zulässige Kontrolle + nutzerübergreifende Behandlung + explizites Oracle + Bereinigung
        ↓
Beleg
  Exakte Anfragen, Antworten, ausgewählter Zustand, Quittungen und Identität des Ziels
        ↓
Ergebnis
  Befund, negatives Ergebnis, nicht eindeutig, blockiert oder nicht versucht
```

## Aktuelle Angriffsabdeckung

Der integrierte Katalog enthält derzeit **72 offensive Testfälle**, kombiniert mit **14
wiederverwendbaren harmlosen Kontrollen**, insgesamt also **86 Fälle**. Ein Fall ist eine konkrete
Kombination von Angriffsdimensionen, nicht notwendigerweise eine eigene Technikfamilie.

- Direkte und indirekte Prompt Injection
- Multi-Turn-Strategien Crescendo, GOAT und Hydra-Select
- Grenzen von Authentifizierung, Sitzung, Identität und Mandanten
- Datenoffenlegung, nutzerübergreifender Zugriff und Extraktion
- Missbrauch von Geschäftslogik, Abläufen, Replay und Zustandsautomaten
- Missbrauch von Tools, Agenten, externen Effekten und Fähigkeiten
- RAG, gespeicherte Inhalte, Memory und Wiedereinspeisung früherer Ausgaben
- Kontext-Derailment, Poison Soup, Token-/Ausgabe-Amplifikation und Leitlinien zum Ressourcenmissbrauch
- Mehrsprachige, registerbezogene, kodierte und verschleierte Varianten
- Browser-, JSON-API- und geprüfte Hybrid-Abläufe

Die Zugehörigkeit zum Katalog bedeutet nicht, dass jeder Angriff auf jedes Ziel anwendbar ist oder
gegen jedes Ziel ausgeführt wird. Berichte unterscheiden zugelassene, versuchte, blockierte, nicht
eindeutige, negative und ungetestete Arbeit.

## Aktuelle Einschränkungen

Was noch unvollständig ist:

- Allgemeine automatische API-Mutation über Nutzer, Sitzungen, Mandanten, Objekt-IDs, Methoden und
  Content-Types hinweg ist noch keine universelle Kampagnenfähigkeit.
- Ein vollständiger Vergleich von Token, Latenz, Tool-Aufrufen, Recovery und Denial-of-Wallet zur
  Scan-Zeit ist noch nicht für jede Kampagne verfügbar.
- Generierter benutzerdefinierter Python- oder Playwright-Code hat keine freigegebene
  Sandbox-Laufzeit.
- Nicht jeder Helfer zur Arm-Generierung ist an die Live-Kampagnenplanung angebunden.
- Eine Reproduktion in einer frischen, isolierten Sitzung ist nicht für jedes Ziel und jeden Befund
  verfügbar.
- Die vergleichende Abdeckung ist weiterhin schmal: Die aktuellen Head-to-Head-Belege stammen aus
  lokalen, nicht offiziellen Reproduktionen und nicht aus offiziellen Leaderboard-Einreichungen.
- Der paketierte Support zielt derzeit auf macOS 14 auf Apple Silicon.

## Benchmarking

Merlin wird als adaptiver Angreifer bewertet, der unter einem festen Budget echte, ausführbare
Schwachstellen findet – nicht anhand eines einzelnen Datensatzes oder einer einzigen ASR-Zahl.
Lokale vergleichende Reproduktionen umfassen inzwischen RIFT-Bench sowie MUZZLE + The Zoo. RIFT
verglich eine Probe mit Merlin auf Flow 0 einer Probe-Familie über 22 lauffähige Systeme, wobei
DeepSeek die Azure-Deployments für Victim und Judge ersetzte und Anpassungen an Abhängigkeiten und
Provider vorgenommen wurden. MUZZLE und Merlin führten jeweils ein angepasstes Gitea-Szenario
gegen dasselbe `deepseek-chat`-Victim und denselben MUZZLE-Ground-Truth-Judge aus, verwendeten aber
unterschiedliche Angreifermodelle und Rundenzahlen.
Die Ergebnisse von Jailbreak Foundry und AgentHarm messen die Anfälligkeit des DeepSeek-Victims,
nicht die Leistung von Merlin. AIRTBench und REDAgentBench bleiben zurückgestellt; die
Benchmark-Daten von REDAgentBench/DianJin-RED stehen unter CC BY-NC 4.0 zuzüglich der geltenden
Upstream-Bedingungen.

**Dies sind lokale, nicht offizielle Reproduktionen mit DeepSeek und offengelegten
Benchmark-Anpassungen, keine offiziellen Leaderboard-Einreichungen.** Nenner und
Evaluierungsverträge unterscheiden sich je nach Track. Siehe
[die Benchmark-Ergebnisse und die Grenzen der Methodik](../benchmarks.md); rohe Benchmark- und
Anbieterausgaben sind nicht im Release enthalten.

## Sicherheit und verantwortungsvoller Einsatz

Merlin ist ein offensives Assessment-Harness für autorisierte Tests. Modelle, Inhalte des Ziels,
Tools, importierte Leitlinien und generierte Prozeduren werden als nicht vertrauenswürdige Eingaben
behandelt. Deterministischer Code behält die Hoheit über Ziele, Zugangsdaten, Tools, Budgets,
folgenreiche Aktionen und Bereinigung.

Setzen Sie Merlin nicht gegen Systeme ein, die Ihnen nicht gehören oder für die Sie keine
ausdrückliche Testerlaubnis haben. Melden Sie Sicherheitsprobleme in Merlin den Maintainern
vertraulich, statt sensible Details in ein öffentliches Issue aufzunehmen.

## Community und Projektstatus

Merlin ist Pre-Release-Software. Issues, Testziele, Angriffsideen, Beiträge zu Adaptern und
reproduzierbare Evaluierungen sind willkommen. Aussagen zur Ausbeute an Befunden sollten Ziel,
Modell, Umfang, Tools, Budget, Nachweisstandard und Vergleichsbedingungen enthalten.

Der First-Party-Code und alle acht mitgelieferten Wissenspakete stehen unter
[Apache-2.0](../../LICENSE). Komponenten von Drittanbietern behalten ihre eigenen Lizenzen; siehe
[Lizenzdetails](../LICENSING.md) und das [Abhängigkeitsinventar](../THIRD-PARTY.md).
Zu Entwicklungsumgebung und Prüfungen siehe [Contributing](../CONTRIBUTING.md),
[die Entwicklungsreferenz](../DEVELOPMENT.md) und die [Quellcode-Architektur](../SOURCE-AND-PACKAGES.md).


### Sitzungs- und Kampagnensteuerung

`/resume` stellt eine gespeicherte Konversation wieder her und verbindet ihre Kampagnen neu.
`/campaign resume ID` setzt eine pausierte Kampagne innerhalb der aktuellen Sitzung fort; es
wechselt nicht die Konversation.
`/interrupt` unterbricht den aktuellen Konversationszug und jeden an diesen Zug angehängten Swarm.
Es bricht eine dauerhafte Kampagne nicht ausdrücklich anhand ihrer ID ab; verwenden Sie dafür
`/campaign cancel ID`.
`/cancel task ID` bricht eine einzelne Aufgabe ab, während andere Kampagnenaufgaben weiterlaufen
können.
`/campaign cancel ID` bricht die Arbeit dieser Kampagne ab und behält ihre Belege.
`/quit` beendet Merlin sauber. Drücken Sie in `/agents` **A** für die gruppierte Agentenübersicht
und **T**, um zum Aufgaben-Arbeitsbereich zurückzukehren.
