# Projet M2 électronique — Suivi de personnes par radar mmWave

**Public** : Master 2 électronique / systèmes embarqués.
**Format** : projet en binôme, 6 séances de 4 h (24 h encadrées) + travail personnel.
**Fil rouge** : un appartement où la lumière suit les occupants, et ne s'éteint
pas sur quelqu'un qui lit sans bouger.

Ce projet couvre une chaîne industrielle complète : capteur RF → liaison série
→ firmware embarqué → réseau → traitement du signal et estimation → logique
système → validation. Les mêmes briques (radar FMCW, Kalman, association de
données) servent dans l'automobile (ADAS), le bâtiment intelligent et la
surveillance de patients.

---

## Compétences visées

| Domaine | Compétence |
|---|---|
| RF / radar | Principe FMCW, résolution distance / vitesse / angle, limites d'une bande ISM |
| Embarqué | UART à haut débit, machine à états de décodage, robustesse aux erreurs |
| Réseau | MQTT, format de trame, latence, gigue |
| Signal / estimation | Changement de repère, filtre de Kalman, distance de Mahalanobis |
| Algorithmique | Association de données (algorithme hongrois), gestion de pistes |
| Système | Machine à états métier, hystérésis, compromis faux positifs / faux négatifs |
| Méthode | Validation par simulation, puis sur banc, puis sur site. Honnêteté des mesures |

---

## Matériel par binôme

- 2 × HLK-LD2450, 1 × HLK-LD2410C, 2 × ESP32 DevKit
- 1 adaptateur USB-UART 3,3 V
- 1 Raspberry Pi (ou PC) avec Mosquitto
- 1 clé Zigbee + 2 ampoules Zigbee (option : 2 LED sur GPIO à la place)
- Mètre ruban, ruban adhésif de sol, rapporteur

---

## Séance 1 — Le radar FMCW, sur papier puis sur table (4 h)

**Cours (1 h)**. Rampe de fréquence (chirp), signal de battement, FFT distance,
FFT Doppler, angle par réseau d'antennes.

**Exercices (1 h)**. Réponses attendues en fin de document.

1. Le LD2450 travaille dans la bande ISM 24,00–24,25 GHz. Calculer la meilleure
   résolution en distance possible, ΔR = c / 2B.
2. Calculer la longueur d'onde à 24,125 GHz. En déduire la fréquence Doppler
   d'une personne qui marche à 1 m/s vers le radar.
3. Deux personnes sont assises côte à côte, à 3 m du radar, séparées de 40 cm.
   Le radar peut-il les séparer en distance ? Quelle autre grandeur peut les
   séparer ? À quelle condition ?
4. Un radar 60 GHz (TI IWR6843) dispose de 4 GHz de bande. Refaire le calcul 1.
   Conclure sur le choix 24 GHz vs 60 GHz pour compter des personnes.
5. Pourquoi un radar Doppler « voit-il » mal une personne immobile ? Quel
   phénomène physique le LD2410C exploite-t-il pour la détecter quand même ?

**Pratique (2 h)**. Brancher un LD2450 sur l'adaptateur USB-UART.

```bash
python -m presence serial --port /dev/ttyUSB0 --sensor ld2450
```

- Relever x, y en vous plaçant sur 5 repères au sol. Estimer biais et écart-type.
- Mesurer le champ de vision réel (angle, portée). Comparer à la fiche technique.
- Rester immobile 60 s à 2 m. Noter combien de temps la cible reste présente.
  **Cette mesure remplace une hypothèse du simulateur** (`p_still_late`).

**Livrable** : tableau des mesures + écart-type de position. Il devient la
valeur `meas_sigma_m` de la configuration.

---

## Séance 2 — Décodage de trame et firmware (4 h)

**Objectif** : écrire le décodeur LD2450 en C++ sans regarder `radar_parsers.h`.

1. À partir du manuel Hi-Link, écrire la machine à états de décodage.
   Contraintes : un octet à la fois, resynchronisation après erreur, pas
   d'allocation dynamique.
2. Valider sur PC avec la trame d'exemple du manuel (`test/test_parsers.cpp`).
3. Ajouter un test : flux coupé au milieu d'une trame, octets parasites, faux
   en-tête. Le décodeur doit rester correct.
4. Porter sur ESP32. Mesurer : nombre de trames par seconde, trames rejetées,
   charge CPU (ou temps passé dans `loop()`).

**Question de réflexion** : à 256 000 bauds, combien d'octets arrivent par
milliseconde ? Que se passe-t-il si `loop()` est bloquée 50 ms par une
reconnexion Wi-Fi ? Quelle est la taille minimale du tampon RX ?

**Livrable** : décodeur testé + mesure du débit de trames.

---

## Séance 3 — Repères et calibration (4 h)

1. Écrire la transformation repère capteur → repère appartement. Vérifier
   avec `test_boresight_direction`.
2. Démontrer que la calibration par moindres carrés d'une transformation
   rigide 2D donne φ = atan2(Σ(aₓb_y − a_y bₓ), Σ(aₓbₓ + a_y b_y)) sur les
   points centrés. (Méthode de Kabsch / Umeyama en 2D.)
3. Calibrer un nœud réel sur 6 repères au sol (`presence calibrate`).
4. Étude de sensibilité : quelle erreur de position à 5 m pour 2°, 5°, 10°
   d'erreur d'angle ? Quelle précision de pose faut-il pour que deux nœuds qui
   voient la même personne donnent des positions à moins de 30 cm ?

**Livrable** : pose calibrée + RMSE + étude de sensibilité.

---

## Séance 4 — Suivi multi-cibles (4 h)

Le cœur algorithmique. Fichier de référence : `hub/presence/tracker.py`.

1. **Kalman** : écrire les matrices F et Q d'un modèle vitesse constante à
   accélération blanche. Justifier la valeur de `accel_sigma`.
2. **Porte d'association** : pourquoi 13,8 pour une distance de Mahalanobis
   à 2 degrés de liberté ? (Loi du χ².)
3. **Association** : comparer l'algorithme glouton et l'algorithme hongrois
   sur un cas où le glouton se trompe (deux personnes qui se croisent).
4. **Expérience guidée** : désactiver une à une ces règles dans le code,
   relancer `python -m presence sim --seed N` sur 10 graines, et remplir le
   tableau :

| Règle désactivée | Comptage exact | Noir avec occupant | Changements d'identité |
|---|---|---|---|
| Aucune (référence) | | | |
| Arrêt de l'extrapolation (`coast_s` = ∞) | | | |
| Porte minimale (`min_gate_m` = 0) | | | |
| Priorité aux pistes confirmées | | | |
| Hystérésis de pièce (`room_hits` = 1) | | | |
| Plus de preuves loin des portes (`confirm_hits_far` = 4) | | | |

Chacune de ces règles a été ajoutée pendant le développement pour corriger
une panne observée en simulation. L'étudiant doit retrouver laquelle.

**Livrable** : tableau rempli + explication d'une panne par règle.

---

## Séance 5 — Logique métier et intégration (4 h)

1. Dessiner la machine à états d'une piste (`provisoire`, `confirmée`,
   `immobile`, `à_la_porte`, `supprimée`) avec toutes les transitions et leurs
   conditions. La comparer au code.
2. Expliquer le principe de **conservation des personnes**. Pourquoi
   remplace-t-il un capteur de présence statique très précis ?
3. Intégration : 2 nœuds dans 2 pièces voisines, hub, MQTT, 2 lampes.
   Mesurer la **latence porte → lumière** (vidéo à 60 images/s : image où le
   pied franchit le seuil vs image où la lampe s'allume). Décomposer :
   radar, Wi-Fi, hub, Zigbee.
4. Régler `off_delay_s` et `static_absent_s`. Argumenter le compromis
   confort / énergie.

**Livrable** : machine à états + mesure de latence décomposée.

---

## Séance 6 — Validation terrain et soutenance (4 h)

Protocole d'essai (30 min par binôme, dans une salle aménagée en 2 pièces) :

| Essai | Attendu |
|---|---|
| 1 personne entre, traverse, ressort | Lumières suivent, 1 piste, identité conservée |
| 1 personne lit 10 min sans bouger | Lumière toujours allumée |
| 2 personnes entrent ensemble, l'une ressort | Lumière reste allumée, comptage 1 |
| 2 personnes se croisent dans une porte | Comptage juste après le croisement |
| Ventilateur oscillant dans le champ | Aucune piste fantôme confirmée |
| Pièce vide 10 min | Lumière éteinte, 0 piste |

**Soutenance (15 min)** : architecture, mesures réelles vs hypothèses du
simulateur, résultats du protocole, **une panne non résolue et son analyse**.

---

## Évaluation

| Critère | Poids |
|---|---|
| Décodeur et firmware robustes, testés | 20 % |
| Calibration et étude de sensibilité | 15 % |
| Compréhension du suivi (tableau séance 4) | 25 % |
| Intégration et latence mesurée | 15 % |
| Protocole terrain et honnêteté des résultats | 15 % |
| Soutenance | 10 % |

Un résultat mauvais mais bien mesuré et bien expliqué vaut mieux qu'un résultat
bon sans protocole.

---

## Prolongements (projets de 2e semestre)

1. **Radar 60 GHz industriel.** Remplacer le LD2450 par un TI IWR6843AOP en
   mode nuage de points. Écrire le clustering (DBSCAN) avant le Kalman.
   Comparer la séparation de deux personnes proches.
2. **FPGA** (Zynq, ex. carte Zybo Z7). Avec un radar qui donne les échantillons
   ADC bruts, implémenter la FFT distance sur la logique programmable et
   comparer latence et consommation avec un traitement sur processeur.
   *Projet lourd : il faut un radar avec sortie ADC brute et sa carte
   d'acquisition. Le LD2450 ne donne que des cibles déjà calculées.*
3. **Chutes de personnes âgées.** Détecter une chute (vitesse verticale, perte
   brutale de hauteur) avec un radar 3D. Sujet à fort enjeu industriel.
4. **Identité.** Associer une piste anonyme à un téléphone (BLE RSSI) pour
   des préférences de lumière par personne.

---

## Réponses aux exercices de la séance 1

1. ΔR = 3·10⁸ / (2 × 250·10⁶) = **0,6 m**.
2. λ = c / f = 3·10⁸ / 24,125·10⁹ ≈ **12,4 mm**. f_D = 2v/λ ≈ 2 × 1 / 0,0124 ≈ **161 Hz**.
3. 40 cm < 0,6 m : **pas séparables en distance**. Elles peuvent l'être en
   **angle** (réseau d'antennes de réception) si leur écart angulaire dépasse
   la résolution angulaire (à 3 m, 40 cm ≈ 7,6°), ou en **vitesse** si leurs
   mouvements diffèrent.
4. ΔR = 3·10⁸ / (2 × 4·10⁹) ≈ **3,75 cm**. Le 60 GHz sépare bien mieux les
   personnes proches. Coût et complexité plus élevés.
5. Le traitement Doppler supprime ce qui ne bouge pas (murs, meubles).
   Une personne immobile ressemble à un meuble. Le LD2410C exploite les
   **micro-mouvements de la respiration** (quelques mm), visibles en phase à
   24 GHz car λ ≈ 12 mm.
