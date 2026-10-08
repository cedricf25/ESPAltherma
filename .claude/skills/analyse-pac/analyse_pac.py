#!/usr/bin/env python3
"""Analyse de la régulation de la PAC Daikin Altherma à partir de l'historique Home Assistant.

Lecture seule (API REST). Jeton dans ~/.ha-token.env (HA_URL, HA_TOKEN).
Réglages de référence (lois d'eau, ΔT, modulation) dans reglages.json à côté du script.

Usage :
  analyse_pac.py                 # dernières 24 h
  analyse_pac.py --heures 72
  analyse_pac.py --debut 2026-10-08T14:00 --fin 2026-10-08T20:00   (heure locale)
"""
import argparse
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ICI = os.path.dirname(os.path.abspath(__file__))

NUM = {
    "ext": "sensor.espaltherma_temperature_air_exterieur",
    "hz": "sensor.espaltherma_frequence_inv",
    "tc_cib": "sensor.espaltherma_temperature_cond_cible",
    "tc": "sensor.espaltherma_haute_pression_temperature_saturation",
    "eev": "sensor.espaltherma_detendeur_electronique_1",
    "liq": "sensor.espaltherma_temperature_tuyau_liquide",
    "refoul": "sensor.espaltherma_temperature_tuyau_refoulement",
    "r1t": "sensor.espaltherma_temp_r1t",
    "r2t": "sensor.espaltherma_temp_r2t",
    "r4t": "sensor.espaltherma_temp_r4t",
    "r5t": "sensor.espaltherma_temp_r5t",
    "lw_add": "sensor.espaltherma_point_reglage_ajoute_lw_ajoute",
    "lw_main": "sensor.espaltherma_point_reglage_lw_principal",
    "debit": "sensor.espaltherma_capteur_debit",
    "pompe": "sensor.espaltherma_pompe_eau_pourcent",
    "chaleur": "sensor.espaltherma_cop_energy_rest",
    "elec": "sensor.shellyem_a4e57cba5164_channel_1_power",
    "sol_dep": "sensor.temperature_depart_sol_temperature",
    "sol_ret": "sensor.temperature_retour_sol_temperature",
    "rt": "sensor.espaltherma_temp_rt",
    "rt_set": "sensor.espaltherma_point_reglage_rt",
    "ecs_set": "sensor.espaltherma_point_reglage_dwh",
}
# Marche/arrêt du chauffage (Onecta) : « heat » ou « off » quand le propriétaire coupe la PAC.
CHAUFFAGE = "climate.room_temperature"
TXT = {
    "mode": "sensor.espaltherma_operation",
    "degivrage": "sensor.espaltherma_degivrage",
    "buh1": "sensor.espaltherma_chauffage_appoint_1",
    "buh2": "sensor.espaltherma_chauffage_appoint_2",
    "huile": "sensor.espaltherma_commande_retour_huile",
}
PAS = 60  # secondes


def env():
    vals = {}
    with open(os.path.expanduser("~/.ha-token.env")) as f:
        for ligne in f:
            if "=" in ligne and not ligne.startswith("#"):
                k, v = ligne.strip().split("=", 1)
                vals[k] = v.strip().strip('"')
    return vals["HA_URL"].rstrip("/"), vals["HA_TOKEN"]


def historique(url, jeton, ids, debut, fin, attributs=False):
    q = {
        "filter_entity_id": ",".join(ids),
        "end_time": fin.isoformat(),
        "significant_changes_only": "0",
    }
    if not attributs:
        q.update(minimal_response="", no_attributes="")
    req = urllib.request.Request(
        f"{url}/api/history/period/{urllib.parse.quote(debut.isoformat())}?{urllib.parse.urlencode(q)}",
        headers={"Authorization": f"Bearer {jeton}"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def ts(x):
    return datetime.fromisoformat(x.get("last_changed") or x["last_updated"]).timestamp()


def en_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def grille(points, t0, n):
    """Valeur en vigueur à chaque pas (dernier état connu ; None si indisponible)."""
    out, i, cur = [], 0, None
    for k in range(n):
        t = t0 + k * PAS
        while i < len(points) and points[i][0] <= t:
            cur = points[i][1]
            i += 1
        out.append(cur)
    return out


def loi(cfg, ext):
    if ext is None:
        return None
    a, b = cfg["ext_froid"], cfg["ext_doux"]
    if ext <= a:
        return cfg["eau_froid"]
    if ext >= b:
        return cfg["eau_doux"]
    return cfg["eau_froid"] + (ext - a) * (cfg["eau_doux"] - cfg["eau_froid"]) / (b - a)


def moy(v):
    v = [x for x in v if x is not None]
    return round(statistics.fmean(v), 2) if v else None


def quantiles(v):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    if len(v) < 5:
        return {"n": len(v), "valeurs": [round(x, 2) for x in v]}
    q = lambda p: round(v[min(len(v) - 1, int(p * len(v)))], 2)
    return {"p10": q(0.1), "med": q(0.5), "p90": q(0.9)}


def part(cond, base):
    n = sum(1 for b in base if b)
    return round(100 * sum(1 for c, b in zip(cond, base) if b and c) / n) if n else None


def sequences(masque, min_len=1):
    """Liste de (début, longueur) des suites de True."""
    res, debut = [], None
    for k, m in enumerate(masque + [False]):
        if m and debut is None:
            debut = k
        elif not m and debut is not None:
            if k - debut >= min_len:
                res.append((debut, k - debut))
            debut = None
    return res


def analyser(debut, fin):
    url, jeton = env()
    reg = json.load(open(os.path.join(ICI, "reglages.json")))
    t0, t1 = debut.timestamp(), fin.timestamp()
    n = int((t1 - t0) // PAS)

    pieces = reg["pieces"]
    num = {**NUM, **{f"piece_{nom}": p["entite"] for nom, p in pieces.items()}}
    brut = historique(url, jeton, list(num.values()) + list(TXT.values()), debut, fin)
    par_id = {s[0]["entity_id"]: s for s in brut if s}
    G, couverture = {}, {}
    for cle, eid in {**num, **TXT}.items():
        pts = [(ts(x), en_float(x["state"]) if cle in num else
                (None if x["state"] in ("unknown", "unavailable") else x["state"]))
               for x in par_id.get(eid, [])]
        G[cle] = grille(pts, t0, n)
        couverture[cle] = round(100 * sum(v is not None for v in G[cle]) / n) if n else 0

    # Chauffage PAC actif, et heures écoulées depuis la dernière remise en route
    # (historique pris en amont de la période pour connaître la dernière mise en marche).
    stab_h = reg["stabilisation_h"]
    amont = debut - timedelta(hours=stab_h)
    hist_ch = (historique(url, jeton, [CHAUFFAGE], amont, fin) or [[]])[0]
    pts_ch = [(ts(x), x["state"]) for x in hist_ch if x["state"] not in ("unknown", "unavailable")]
    actif, connu, depuis_marche, remises = [], [], [], []
    i, etat, t_on = 0, None, amont.timestamp() - stab_h * 3600  # inconnu au départ : supposé stabilisé
    for k in range(n):
        t = t0 + k * PAS
        while i < len(pts_ch) and pts_ch[i][0] <= t:
            nouvel = pts_ch[i][1]
            if nouvel == "heat" and etat not in (None, "heat"):
                t_on = pts_ch[i][0]
                if t_on >= t0:
                    remises.append(datetime.fromtimestamp(t_on).strftime("%d/%m %H:%M"))
            etat = nouvel
            i += 1
        actif.append(etat == "heat")
        connu.append(etat is not None)
        depuis_marche.append((t - t_on) / 3600)
    couverture["chauffage_pac"] = round(100 * sum(connu) / n) if n else 0

    hzmin = reg["references"]["compresseur_min_hz"]
    marche = [h is not None and h > 0 for h in G["hz"]]
    # Mode inconnu (capteur indisponible) : compté comme chauffage, signalé par la couverture.
    chauffage = [m and (md is None or md.lower().startswith("heat")) and dg != "ON"
                 for m, md, dg in zip(marche, G["mode"], G["degivrage"])]
    diff = lambda a, b: [x - y if x is not None and y is not None else None for x, y in zip(G[a], G[b])]
    sel = lambda serie, masque: [v for v, m in zip(serie, masque) if m]

    loi_rad = [loi(reg["loi_eau"]["radiateurs"], e) for e in G["ext"]]
    loi_pl = [loi(reg["loi_eau"]["plancher"], e) for e in G["ext"]]
    mod_rad = [a - b if a is not None and b is not None else None for a, b in zip(G["lw_add"], loi_rad)]
    mod_pl = [a - b if a is not None and b is not None else None for a, b in zip(G["lw_main"], loi_pl)]
    ecart_eau = diff("r2t", "lw_add")
    ecart_sol = diff("sol_dep", "lw_main")
    ecart_rt = diff("rt", "rt_set")

    # Pièces : écart à la consigne de la vanne (étage) ou de la Madoka (bas).
    ecart_piece = {}
    for nom, p in pieces.items():
        cons = G["rt_set"] if p["consigne"] == "madoka" else [p["consigne"]] * n
        ecart_piece[nom] = [x - c if x is not None and c is not None else None
                            for x, c in zip(G[f"piece_{nom}"], cons)]

    # Poêle à bois probable : une pièce du bas nettement au-dessus de la consigne Madoka.
    seuil = reg["bois"]["seuil_k"]
    bois = [any((ecart_piece[nom][k] or 0) > seuil for nom in reg["bois"]["pieces"]) for k in range(n)]
    # Régime analysable pour les lois d'eau : PAC en chauffe, stabilisée, sans bois.
    regime = [a and d >= stab_h and not b for a, d, b in zip(actif, depuis_marche, bois)]

    def plus_froide(zone):
        """Écart minute par minute de la pièce repère la plus en retard sur sa consigne."""
        noms = [nom for nom, p in pieces.items() if p["zone"] == zone and p["role"] == "repere"]
        out = []
        for k in range(n):
            v = [ecart_piece[nom][k] for nom in noms if ecart_piece[nom][k] is not None]
            out.append(min(v) if v else None)
        return out
    froide_rad, froide_pl = plus_froide("radiateurs"), plus_froide("plancher")
    ecart_tc = diff("tc", "tc_cib")
    dt_pac = diff("r2t", "r4t")
    dt_sol = diff("sol_dep", "sol_ret")
    sous_ref = diff("tc", "liq")

    # Cycles compresseur
    runs = sequences(marche)
    arrets = sequences([not m for m in marche])
    demarrages = sum(1 for d, _ in runs if d > 0)
    h_marche = sum(marche) * PAS / 3600

    # Épisodes « bloqué au minimum » : compresseur au plancher, condensation mesurée au-dessus
    # de la cible et eau qui décroche de sa consigne.
    bloque = [m and h <= hzmin + 2 and (e or 0) > 1 and (w or 0) < -2
              for m, h, e, w in zip(chauffage, [h or 0 for h in G["hz"]], ecart_tc, ecart_eau)]
    depassement = [m and (w or 0) > 1 for m, w in zip(chauffage, ecart_eau)]
    heure = lambda k: datetime.fromtimestamp(t0 + k * PAS).strftime("%d/%m %H:%M")

    def episodes(masque, min_len):
        res = []
        for d, l in sequences(masque, min_len):
            res.append({
                "debut": heure(d), "minutes": l,
                "tc_moins_cible_max": round(max((ecart_tc[k] or 0) for k in range(d, d + l)), 1),
                "eau_moins_consigne_min": round(min((ecart_eau[k] or 0) for k in range(d, d + l)), 1),
                "eev_debut_fin": [G["eev"][d], G["eev"][d + l - 1]],
                "sous_refroidissement_max": round(max((sous_ref[k] or 0) for k in range(d, d + l)), 1),
            })
        return res

    # Énergie et COP (pondéré par l'énergie, pas moyenne des COP instantanés)
    def cop(masque):
        q = sum(c for c, m in zip(G["chaleur"], masque) if m and c is not None)
        e = sum(p for p, m in zip(G["elec"], masque) if m and p is not None)
        return round(q / e, 2) if e > 0 else None

    elec_kwh = sum(p for p in G["elec"] if p is not None) * PAS / 3.6e6
    chaleur_kwh = sum(c for c, m in zip(G["chaleur"], marche) if m and c is not None) * PAS / 3.6e6

    # Par tranche de température extérieure (3 °C)
    tranches = {}
    for k in range(n):
        e = G["ext"][k]
        if e is None:
            continue
        b = int((e // 3) * 3)
        tranches.setdefault(b, []).append(k)
    par_ext = []
    for b in sorted(tranches):
        ks = tranches[b]
        dans = set(ks)
        pick = lambda s: [s[k] for k in ks]
        par_ext.append({
            "ext": f"{b}..{b + 3}", "heures": round(len(ks) / 60, 1),
            "marche_%": round(100 * sum(marche[k] for k in ks) / len(ks)),
            "hz_moy": moy([G["hz"][k] for k in ks if marche[k]]),
            "heures_regime": round(sum(regime[k] for k in ks) / 60, 1),
            "cop": cop([chauffage[k] and regime[k] and k in dans for k in range(n)]),
            "eau_moins_consigne_rad": moy([ecart_eau[k] for k in ks if chauffage[k] and regime[k]]),
            "depart_sol_moins_consigne": moy([ecart_sol[k] for k in ks if marche[k] and regime[k]]),
            "plus_froide_bas": moy([froide_pl[k] for k in ks if regime[k]]),
            "plus_froide_etage": moy([froide_rad[k] for k in ks if regime[k]]),
            "loi_rad": round(loi(reg["loi_eau"]["radiateurs"], b + 1.5), 1),
            "loi_plancher": round(loi(reg["loi_eau"]["plancher"], b + 1.5), 1),
        })

    # Indices pour les réglages (à interpréter, pas des verdicts)
    indices = []
    h_regime = sum(regime) / 60
    if h_regime < 12:
        indices.append(f"PÉRIODE INSUFFISANTE : {h_regime:.1f} h seulement de chauffage PAC stabilisé sans bois → "
                       f"ne pas juger les lois d'eau sur cette période.")
    else:
        val = lambda s: [v for v in sel(s, regime) if v is not None]
        et_ok = part([v >= -0.3 for v in val(froide_rad)], [True] * len(val(froide_rad)))
        et_marge = part([v >= 0.5 for v in val(froide_rad)], [True] * len(val(froide_rad)))
        bas_ok = part([v >= -0.3 for v in val(froide_pl)], [True] * len(val(froide_pl)))
        bas_marge = part([v >= 0.5 for v in val(froide_pl)], [True] * len(val(froide_pl)))
        eau_bas = moy(sel(ecart_eau, [c and r for c, r in zip(chauffage, regime)]))
        sol_ecart = moy(sel(ecart_sol, [m and r for m, r in zip(marche, regime)]))
        if et_marge is not None and et_marge >= 70:
            indices.append(f"RADIATEURS : même la pièce de l'étage la plus en retard dépasse sa vanne de 0,5 K "
                           f"{et_marge} % du temps → les vannes freinent, marge pour baisser la loi radiateurs.")
        if et_ok is not None and et_ok < 50 and (eau_bas or 0) > -1:
            indices.append(f"RADIATEURS : une pièce de l'étage n'atteint sa vanne que {et_ok} % du temps avec une eau "
                           f"à la loi ({eau_bas} K) → loi radiateurs peut-être trop basse (vérifier quelle pièce).")
        if bas_marge is not None and bas_marge >= 70 and (sol_ecart or 0) > -0.5:
            indices.append(f"PLANCHER : toutes les pièces du bas dépassent la consigne Madoka de 0,5 K {bas_marge} % "
                           f"du temps → marge pour baisser la loi plancher.")
        if bas_ok is not None and bas_ok < 50 and (sol_ecart or 0) > -0.5:
            indices.append(f"PLANCHER : une pièce du bas n'atteint la consigne Madoka que {bas_ok} % du temps, départ "
                           f"à la loi → loi plancher peut-être trop basse.")
    if h_marche > 1 and demarrages / h_marche > 1.5:
        indices.append(f"CYCLES COURTS : {demarrages} démarrages pour {h_marche:.1f} h de marche.")
    if any((v or "") == "ON" for v in G["buh1"] + G["buh2"]):
        indices.append("APPOINT ÉLECTRIQUE : le BUH s'est enclenché sur la période.")
    mr = [v for v in sel(mod_rad, chauffage) if v is not None]
    if mr and max(abs(x) for x in mr) > 0.8:
        indices.append(f"MODULATION : consigne radiateurs décalée de la loi entre {min(mr):+.1f} et {max(mr):+.1f} K "
                       f"(max réglé ±{reg['modulation_max']}).")

    return {
        "periode": {"debut": debut.astimezone().strftime("%d/%m %H:%M"), "fin": fin.astimezone().strftime("%d/%m %H:%M"),
                    "heures": round(n / 60, 1)},
        "reglages": reg,
        "couverture_%": couverture,
        "contexte": {
            "ext": quantiles(G["ext"]),
            "consigne_madoka": moy(G["rt_set"]),
            "modes_vus_en_marche_min": {m: sum(1 for x, mm in zip(G["mode"], marche) if mm and x == m)
                                       for m in set(G["mode"]) if m},
        },
        "regime": {
            "heures_pac_en_chauffe": round(sum(actif) / 60, 1),
            "remises_en_route": remises,
            "heures_bois_probable": round(sum(bois) / 60, 1),
            "periodes_bois_probable": [f"{heure(d)} ({l} min)" for d, l in sequences(bois, 30)],
            "heures_analysables": round(h_regime, 1),
            "_note": f"Lois d'eau, pièces et COP chauffage calculés sur les seules heures analysables "
                     f"(PAC en chauffe depuis ≥ {stab_h} h, sans bois probable).",
        },
        "pieces": {nom: {"zone": p["zone"], "role": p["role"],
                         "consigne": p["consigne"] if p["consigne"] != "madoka" else moy(sel(G["rt_set"], regime)),
                         "moy": moy(sel(G[f"piece_{nom}"], regime)),
                         "moy_toute_periode": moy(G[f"piece_{nom}"]),
                         "ecart": quantiles(sel(ecart_piece[nom], regime))}
                   for nom, p in pieces.items()},
        "compresseur": {
            "heures_marche": round(h_marche, 1), "demarrages": demarrages,
            "cycle_marche_min": quantiles([l for _, l in runs]),
            "arret_min": quantiles([l for d, l in arrets if d > 0]),
            "hz": quantiles(sel(G["hz"], marche)),
            "temps_au_minimum_%": part([(h or 0) <= hzmin + 2 for h in G["hz"]], marche),
            "episodes_bloque_au_min": episodes(bloque, 10),
            "minutes_eau_au_dessus_consigne": sum(depassement),
        },
        "boucle_frigo": {
            "cond_mesuree_moins_cible": quantiles(sel(ecart_tc, chauffage)),
            "sous_refroidissement": quantiles(sel(sous_ref, chauffage)),
            "refoulement": quantiles(sel(G["refoul"], marche)),
            "detendeur": quantiles(sel(G["eev"], marche)),
        },
        "loi_eau": {
            "modulation_radiateurs": quantiles(sel(mod_rad, chauffage)),
            "modulation_plancher": quantiles(sel(mod_pl, chauffage)),
            "eau_r2t_moins_consigne_rad": quantiles(sel(ecart_eau, [c and r for c, r in zip(chauffage, regime)])),
            "depart_sol_moins_consigne_plancher": quantiles(sel(ecart_sol, [m and r for m, r in zip(marche, regime)])),
            "plus_froide_bas_moins_consigne": quantiles(sel(froide_pl, regime)),
            "plus_froide_etage_moins_vanne": quantiles(sel(froide_rad, regime)),
        },
        "delta_t": {
            "pac_r2t_r4t": quantiles(sel(dt_pac, chauffage)),
            "plancher_shelly": quantiles(sel(dt_sol, marche)),
            "debit_l_min": quantiles(sel(G["debit"], marche)),
            "pompe_principale_%": quantiles(sel(G["pompe"], marche)),
        },
        "energie": {
            "elec_kwh": round(elec_kwh, 2), "chaleur_kwh": round(chaleur_kwh, 2),
            "cop_chauffage": cop([c and r for c, r in zip(chauffage, regime)]),
            "cop_chauffage_toutes_heures": cop([c and a for c, a in zip(chauffage, actif)]),
            "cop_global": cop(marche),
            "temp_eau_moy_en_marche": moy(sel(G["r2t"], [c and a for c, a in zip(chauffage, actif)])),
        },
        "ecs": {"ballon": quantiles(G["r5t"]), "consigne": moy(G["ecs_set"])},
        "degivrages": {"nombre": len(sequences([v == "ON" for v in G["degivrage"]])),
                       "minutes": sum(1 for v in G["degivrage"] if v == "ON")},
        "par_temperature_exterieure": par_ext,
        "indices": indices,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--heures", type=float, default=24)
    p.add_argument("--debut")
    p.add_argument("--fin")
    a = p.parse_args()
    loc = datetime.now().astimezone().tzinfo
    fin = datetime.fromisoformat(a.fin).replace(tzinfo=loc) if a.fin else datetime.now(timezone.utc)
    debut = datetime.fromisoformat(a.debut).replace(tzinfo=loc) if a.debut else fin - timedelta(hours=a.heures)
    json.dump(analyser(debut.astimezone(timezone.utc), fin.astimezone(timezone.utc)),
              sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
