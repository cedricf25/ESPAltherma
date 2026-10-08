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
TXT = {
    "mode": "sensor.espaltherma_operation",
    "degivrage": "sensor.espaltherma_degivrage",
    "buh1": "sensor.espaltherma_chauffage_appoint_1",
    "buh2": "sensor.espaltherma_chauffage_appoint_2",
    "huile": "sensor.espaltherma_commande_retour_huile",
}
ETAGE = "climate.thermostat_1er_etage"
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

    brut = historique(url, jeton, list(NUM.values()) + list(TXT.values()), debut, fin)
    par_id = {s[0]["entity_id"]: s for s in brut if s}
    G, couverture = {}, {}
    for cle, eid in {**NUM, **TXT}.items():
        pts = [(ts(x), en_float(x["state"]) if cle in NUM else
                (None if x["state"] in ("unknown", "unavailable") else x["state"]))
               for x in par_id.get(eid, [])]
        G[cle] = grille(pts, t0, n)
        couverture[cle] = round(100 * sum(v is not None for v in G[cle]) / n) if n else 0
    try:
        et = historique(url, jeton, [ETAGE], debut, fin, attributs=True)[0]
        pts_t = [(ts(x), en_float(x["attributes"].get("current_temperature"))) for x in et]
        pts_c = [(ts(x), en_float(x["attributes"].get("temperature"))) for x in et]
        G["etage"], G["etage_set"] = grille(pts_t, t0, n), grille(pts_c, t0, n)
    except Exception:
        G["etage"] = G["etage_set"] = [None] * n

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
    ecart_etage = diff("etage", "etage_set")
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
            "cop": cop([chauffage[k] and k in dans for k in range(n)]),
            "eau_moins_consigne_rad": moy([ecart_eau[k] for k in ks if chauffage[k]]),
            "depart_sol_moins_consigne": moy([ecart_sol[k] for k in ks if marche[k]]),
            "piece_madoka_moins_consigne": moy(pick(ecart_rt)),
            "etage_moins_consigne": moy(pick(ecart_etage)),
            "loi_rad": round(loi(reg["loi_eau"]["radiateurs"], b + 1.5), 1),
            "loi_plancher": round(loi(reg["loi_eau"]["plancher"], b + 1.5), 1),
        })

    # Indices pour les réglages (à interpréter, pas des verdicts)
    indices = []
    et_ok = part([v is not None and v >= -0.3 for v in ecart_etage], [v is not None for v in ecart_etage])
    rt_ok = part([v is not None and v >= -0.3 for v in ecart_rt], [v is not None for v in ecart_rt])
    eau_bas = moy(sel(ecart_eau, chauffage))
    sol_ecart = moy(sel(ecart_sol, marche))
    if et_ok is not None and eau_bas is not None:
        if et_ok >= 70 and eau_bas < -1.5:
            indices.append(f"RADIATEURS : l'étage tient sa consigne {et_ok} % du temps alors que l'eau est en moyenne "
                           f"{eau_bas} K sous la loi → loi radiateurs probablement trop haute.")
        if et_ok < 50 and eau_bas > -1:
            indices.append(f"RADIATEURS : l'étage n'atteint sa consigne que {et_ok} % du temps avec une eau à la loi "
                           f"({eau_bas} K) → loi radiateurs peut-être trop basse.")
    if rt_ok is not None and sol_ecart is not None:
        if rt_ok >= 80 and sol_ecart > -0.5:
            indices.append(f"PLANCHER : la pièce Madoka tient sa consigne {rt_ok} % du temps → marge pour baisser la loi plancher.")
        if rt_ok < 50 and sol_ecart > -0.5:
            indices.append(f"PLANCHER : la pièce Madoka n'atteint sa consigne que {rt_ok} % du temps, départ à la loi → loi plancher peut-être trop basse.")
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
            "piece_madoka": moy(G["rt"]), "consigne_madoka": moy(G["rt_set"]),
            "etage": moy(G["etage"]), "consigne_etage": moy(G["etage_set"]),
            "modes_vus_en_marche_min": {m: sum(1 for x, mm in zip(G["mode"], marche) if mm and x == m)
                                       for m in set(G["mode"]) if m},
        },
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
            "eau_r2t_moins_consigne_rad": quantiles(sel(ecart_eau, chauffage)),
            "depart_sol_moins_consigne_plancher": quantiles(sel(ecart_sol, marche)),
            "piece_madoka_moins_consigne": quantiles(ecart_rt),
            "etage_moins_consigne": quantiles(ecart_etage),
        },
        "delta_t": {
            "pac_r2t_r4t": quantiles(sel(dt_pac, chauffage)),
            "plancher_shelly": quantiles(sel(dt_sol, marche)),
            "debit_l_min": quantiles(sel(G["debit"], marche)),
            "pompe_principale_%": quantiles(sel(G["pompe"], marche)),
        },
        "energie": {
            "elec_kwh": round(elec_kwh, 2), "chaleur_kwh": round(chaleur_kwh, 2),
            "cop_chauffage": cop(chauffage), "cop_global": cop(marche),
            "temp_eau_moy_en_marche": moy(sel(G["r2t"], chauffage)),
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
