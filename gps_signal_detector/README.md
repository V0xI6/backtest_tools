# Détecteur de traceurs GPS

Outil défensif de contre-surveillance : il repère les balises qui **vous
suivent**, surveille le brouillage du signal GNSS, et cherche les émetteurs
périodiques des traceurs cellulaires.

Trois moyens de détection complémentaires, parce qu'aucun ne suffit seul :

| Module | Ce qu'il détecte | Matériel nécessaire |
|---|---|---|
| **BLE** | AirTag, Tile, SmartTag, balises Google FMDN, traceurs à interface Bluetooth | adaptateur Bluetooth |
| **GNSS** | brouillage et leurrage du signal satellite | récepteur GPS (ou gpsd) |
| **RF** | traceurs 2G/4G sans Bluetooth, par leurs salves périodiques | clé RTL-SDR |

---

## Cadre d'utilisation

Cet outil sert à découvrir un traceur **posé sur vous, votre véhicule ou vos
affaires**. C'est la même fonction que « Tracker Detect » chez Apple ou les
alertes anti-pistage d'Android, en plus complet et sans dépendre d'un
service tiers.

Le balayage RF est en **réception seule** : l'outil n'émet rien, ne brouille
rien et ne cherche à neutraliser aucun appareil. Détenir ou utiliser un
brouilleur est un délit dans la plupart des pays, dont la France.

En France, poser un traceur sur autrui à son insu relève de l'atteinte à la
vie privée (art. 226-1 du code pénal). Si vous trouvez un appareil : ne le
détruisez pas, il constitue une preuve.

---

## Prise en main

Le cœur du détecteur n'utilise que la bibliothèque standard. Sans aucune
installation, vous pouvez déjà voir l'outil travailler sur un trajet
simulé :

```bash
python -m gps_signal_detector demo
```

Le scénario reproduit une immobilisation au domicile puis un trajet de
29 km, avec deux vrais traceurs cachés parmi trois appareils anodins. Le
rapport se termine par la vérité terrain, ce qui permet de vérifier que le
détecteur ne se trompe pas.

### Pour l'utiliser en vrai

```bash
pip install bleak            # scan BLE (indispensable)
pip install pyserial         # récepteur GPS sur port série (facultatif)
pip install pyrtlsdr numpy   # balayage RF avec une clé RTL-SDR (facultatif)
```

```bash
# 1. Déclarez votre domicile : un appareil qui n'en sort jamais n'est pas
#    un traceur, c'est le voisinage.
python -m gps_signal_detector zone add Domicile 48.8566 2.3522

# 2. Roulez trente minutes en emportant l'ordinateur, par un itinéraire
#    inhabituel. Ctrl-C pour arrêter.
python -m gps_signal_detector scan --position gpsd --interval 30

# 3. Recoupez plusieurs trajets : c'est là que se joue la détection.
python -m gps_signal_detector analyse --since 72 --out-html rapport.html
```

Sources de position acceptées par `--position` : `gpsd`, `gpsd:hôte:port`,
`/dev/ttyACM0` (récepteur série), un fichier NMEA à rejouer, ou
`48.8566,2.3522` pour une position fixe.

> **Sans position, la détection est très affaiblie.** Les deux critères les
> plus décisifs — nombre de zones distinctes et distance parcourue —
> deviennent inexploitables, et le rapport le signale.

---

## Comment se décide un verdict

Un traceur se distingue d'un objet connecté quelconque par une chose : il
reste présent alors que **vous** vous déplacez. Le téléviseur du voisin est
vu longtemps, mais toujours au même endroit. L'enceinte croisée en ville
est vue partout, mais quelques secondes. Le traceur, lui, cumule durée,
distance et lieux distincts.

Chaque critère est noté séparément — le verdict reste explicable :

| Critère | Poids | Ce qu'il mesure |
|---|---:|---|
| Signature | 25 | famille de traceur reconnue, pondérée par la confiance |
| Mode séparé | 10 | balise émettant « séparée de son propriétaire » |
| Durée | 15 | temps de présence total |
| Zones distinctes | 25 | nombre de lieux séparés où l'émetteur réapparaît |
| Distance | 15 | trajet parcouru en sa présence |
| Proximité | 5 | puissance reçue médiane |
| Régularité | 5 | proportion de scans auxquels il répond |

Score total sur 100 → `aucune` (<25), `faible` (25), `suspect` (50),
`probable` (70), `confirmé` (85).

Quatre garde-fous limitent les fausses alertes, parce qu'accuser à tort a
un coût :

- **Présence trop brève** : en roulant, une balise de bord de route croisée
  trente secondes traverse plusieurs « zones » sans rien suivre. Les
  critères spatiaux ne comptent qu'à partir d'une présence installée.
- **Zones de confiance** : un émetteur jamais vu hors de votre domicile
  voit son score fortement réduit.
- **Trop peu de détections** : sous trois détections, l'outil refuse de
  conclure.
- **Appareils de confiance** : vos propres objets (`trust`) sont écartés.

### Rotation des adresses MAC

Les AirTag et balises FMDN changent d'adresse Bluetooth toutes les quinze
minutes environ. Sans recollage, un traceur suivi deux heures ressemblerait
à huit appareils anodins vus un quart d'heure chacun.

Le recollage n'a lieu que si toutes les conditions tiennent : même famille
de traceur, aucun chevauchement temporel réel, rotation assez rapprochée,
puissance comparable, et positions compatibles avec la distance que
*vous* avez pu parcourir entre-temps. Deux appareils inconnus ne sont
jamais fusionnés par défaut.

---

## Commandes

| Commande | Rôle |
|---|---|
| `interface` | **écoute permanente avec affichage graphique dans le navigateur** |
| `traque` | la même chose dans le terminal, pour qui préfère |
| `demo` | analyse un scénario simulé, sans matériel |
| `scan` | scan BLE en direct, avec position et enregistrement |
| `analyse` | rejoue les observations enregistrées (`--since`, `--session`) |
| `sessions` | liste les sessions du journal |
| `gnss SOURCE` | santé du signal GNSS (fichier NMEA ou port série) |
| `rf` | balayage des bandes montantes (RTL-SDR) |
| `bands` | liste les bandes surveillées |
| `trust` / `devices` | gère les appareils de confiance |
| `zone add/list/rm` | gère les zones de confiance |

Toutes les commandes d'analyse acceptent `--json`, `--out-json FICHIER`,
`--out-html FICHIER`, `--all` et `--limit`.

### Interface graphique

C'est la façon la plus simple d'utiliser l'outil. Une page s'ouvre dans
votre navigateur et se met à jour toute seule pendant que vous vous
déplacez.

```bash
python -m gps_signal_detector interface --demo    # sans matériel, pour voir
python -m gps_signal_detector interface           # écoute réelle
```

Le serveur est local (`127.0.0.1`) et la page ne charge aucune ressource
extérieure : rien ne sort de votre machine.

**Aucun jargon à l'écran.** Le signal est exprimé en pourcentage, pas en
dBm ; les traceurs portent des noms courants (« Traceur Apple AirTag »,
« Réseau Tile ») plutôt que leurs identifiants internes ; la tendance se lit
en toutes lettres (« Vous vous rapprochez »). Les valeurs brutes restent
accessibles sous le repli **Détails techniques**, pour qui les veut.

Deux écrans :

1. **La liste** — tout ce qui émet autour de vous, du plus proche au plus
   loin, avec une pastille d'alerte sur les traceurs identifiés et une
   phrase d'explication quand un objet signale sa position à distance.
2. **La recherche** — un grand pourcentage, une jauge, la tendance en
   couleur, la courbe de la dernière minute, et le bouton **Marquer cet
   endroit** qui relève votre position pour retrouver la zone la plus
   chaude.

Le code couleur ne compte que trois niveaux — bleu, orange, rouge — et
chacun est toujours accompagné de son texte : la couleur ne porte jamais
seule le sens, y compris pour un œil daltonien.

### Traque en temps réel (terminal)

Les autres commandes répondent à « suis-je suivi ? ». Celle-ci répond à
« **où est-il ?** ». Le scan tourne en continu, l'écran se rafraîchit
plusieurs fois par seconde, et rien n'est à relancer : vous déplacez
l'ordinateur et vous regardez la jauge.

```bash
python -m gps_signal_detector traque --demo    # sans matériel, pour voir
python -m gps_signal_detector traque           # écoute réelle
```

Vous obtenez d'abord la liste des appareils entendus, classés par puissance.
Vous en choisissez un, et l'écran de traque affiche la puissance lissée, une
jauge, la tendance (**vous chauffez / vous refroidissez**), la bande de
proximité et la courbe des soixante dernières secondes.

| Touche | Effet |
|---|---|
| `↑` `↓` | choisir un appareil |
| `entrée` | lancer la traque |
| `m` | relever un point à l'endroit où vous êtes |
| `r` | recalibrer (changement de pièce ou de véhicule) |
| `tab` | appareil suivant |
| `q` | retour à la liste, puis quitter |

**La méthode.** Ne cherchez pas à lire une position sur l'écran, il n'y en a
pas. Balayez lentement la zone et regardez la tendance : c'est le
rapprochement qui vous renseigne, pas la valeur absolue. Aux endroits
intéressants, appuyez sur `m` : la liste des points relevés vous donne la
zone la plus chaude, ce qui remplace la direction que la radio ne fournit
pas.

**Trois limites à connaître**, sinon vous chercherez au mauvais endroit :

- **Aucune direction.** Une antenne unique ne mesure pas d'angle. L'outil ne
  vous dira jamais « à gauche » — d'où les points relevés, qui sont une
  triangulation à la main.
- **Aucune précision centimétrique.** Elle demanderait de l'UWB (la puce U1
  d'un iPhone), qu'aucun ordinateur ne possède. Le RSSI se trompe couramment
  de 50 à 100 % en distance absolue ; il n'est fiable qu'en *variation*. Et
  très près, il sature : les derniers centimètres sont les plus difficiles.
- **La cadence d'émission borne la réactivité.** Un AirTag n'émet que toutes
  les deux secondes ; la jauge ne peut pas être plus rapide que lui. Bougez
  lentement.

**Si l'écran affiche l'avertissement « votre plateforme déduplique les
annonces »**, c'est que le système ne signale chaque appareil qu'une fois et
que le RSSI ne bougera jamais. C'est le comportement de CoreBluetooth sur
macOS quand l'option `AllowDuplicates` n'est pas active. Relancez ainsi :

```bash
python -m gps_signal_detector traque --restart-scan 2
```

### Balayage RF

Un traceur 2G/4G sans Bluetooth reste invisible au scan BLE. Mais pour
transmettre sa position, il doit **émettre** : par salves, sur les bandes
montantes, à intervalle régulier. C'est cette régularité que l'outil
cherche — pas la puissance, car le téléphone du conducteur émet bien plus
fort, mais n'importe quand.

```bash
python -m gps_signal_detector rf --passes 10 --interval 120
```

Une clé RTL-SDR plafonne vers 1,77 GHz : elle couvre les bandes 800/900 MHz
et 1800 MHz, où se trouve l'essentiel des traceurs. Les bandes 2100 et
2600 MHz demandent un SDR à plus large couverture (`--all-bands`).

### Surveillance GNSS

```bash
python -m gps_signal_detector gnss /dev/ttyACM0
python -m gps_signal_detector gnss trace.nmea
```

Un brouilleur écrase toutes les porteuses d'un coup : le rapport
signal/bruit s'effondre alors que les satellites restent visibles. Un
leurre, à l'inverse, fabrique des signaux **forts et trop réguliers** — un
vrai ciel varie avec l'élévation des satellites. L'outil distingue les deux
cas, là où un seuil naïf sur l'uniformité confondrait un brouillage avec un
leurre.

---

## Où chercher un traceur

Si le rapport conclut à `probable` ou `confirmé`, les emplacements
classiques sont : pare-chocs et passages de roue (boîtiers aimantés), sous
les sièges, coffre et roue de secours, plaque d'attelage, boîtier OBD sous
le volant, doublure de sac. Un traceur câblé sur la batterie doit être
retiré par un professionnel.

Mettez-vous en lieu sûr avant d'inspecter, exportez le rapport horodaté
(`--out-json`) et conservez-le.

---

## Limites connues

- Un traceur **purement cellulaire, sans Bluetooth**, est invisible au scan
  BLE : c'est le rôle du balayage RF, qui demande une clé SDR.
- Le recollage des adresses tournantes est **heuristique**. Il est réglé
  pour être prudent : il préfère laisser un traceur éclaté en plusieurs
  pistes que fusionner deux appareils distincts.
- Le type d'adresse BLE (publique / privée) n'est qu'une **présomption**
  déduite des bits de poids fort, sauf si l'adaptateur le fournit.
- Les signatures fondées sur le **nom annoncé** (traceurs cellulaires,
  Chipolo, Pebblebee) sont marquées « confiance faible » : un nom se
  falsifie.
- Les seuils par défaut visent un **trajet routier**. À pied, abaissez
  `--min-duration` et le rayon de zone.

---

## Développement

```bash
python -m unittest discover -s gps_signal_detector/tests -t .
```

204 tests, bibliothèque standard uniquement (le test d'analyse spectrale se
saute tout seul si numpy est absent). Le rendu du mode traque produit des
lignes pures et la traduction de l'interface web est une fonction pure : les
deux se vérifient sans terminal ni navigateur.

| Fichier | Rôle |
|---|---|
| `models.py` | structures de données, niveaux de menace |
| `geo.py` | distances, regroupement en zones, longueur de trajet |
| `signatures.py` | base de signatures BLE et décodage Find My |
| `tracking_analyzer.py` | moteur de décision et recollage des identités |
| `live_state.py` | état temps réel : lissage, tendance, bandes de proximité |
| `web_ui.py` | traduction en langage courant et serveur de l'interface |
| `web/index.html` | la page : liste, recherche, courbe (aucune ressource externe) |
| `hunt_view.py` | rendu du mode traque (lignes pures, testables) |
| `hunt.py` | boucle temps réel et affichage curses |
| `ble_scanner.py` | acquisition BLE (bleak) |
| `position.py` | sources de position : NMEA, gpsd, fixe |
| `gnss_monitor.py` | analyse NMEA, brouillage et leurrage |
| `rf_sweep.py` | balayage RF et recherche de périodicité |
| `storage.py` | journal SQLite |
| `report.py` | rapports texte, JSON et HTML |
| `simulator.py` | scénarios synthétiques |
| `cli.py` | ligne de commande |

En bibliothèque :

```python
from gps_signal_detector import TrackingAnalyzer, render_text

verdicts = TrackingAnalyzer().analyze(observations, scan_times, safe_zones)
print(render_text(verdicts, observations, scan_times))
```

Pour ajouter vos propres signatures sans toucher au code, écrivez-les en
JSON et chargez-les avec `signatures.load_custom_signatures()`.
