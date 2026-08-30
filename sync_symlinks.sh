#!/bin/bash
# =============================================================================
#  ISOWatcher — sync_symlinks.sh
#  Crée les liens symboliques depuis les ISOs vers proxmox-view/template/iso/
#  S'exécute DIRECTEMENT sur Unraid (filesystem XFS natif = symlinks OK)
#  Appelé par ISOWatcher via SSH depuis le container Docker
# =============================================================================

# ── Configuration ─────────────────────────────────────────────────────────────
SOURCE_DIR="${ISOWATCHER_SOURCE_DIR:-/mnt/user/isos/isos}"
DEST_DIR="${ISOWATCHER_DEST_DIR:-/mnt/user/isos/proxmox-view/template/iso}"
LOG_FILE="${ISOWATCHER_LOG:-/mnt/user/isos/isowatcher/sync_symlinks.log}"
# =============================================================================

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG_FILE"; }

mkdir -p "$DEST_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

log "========================================"
log "Début de la synchronisation des symlinks"
log "SOURCE : $SOURCE_DIR"
log "DEST   : $DEST_DIR"
log "========================================"

created=0; skipped=0; updated=0; removed=0; conflicts=0

# ── Création / mise à jour des symlinks ───────────────────────────────────────
while IFS= read -r -d '' iso_path; do

    iso_name=$(basename "$iso_path")
    symlink_path="$DEST_DIR/$iso_name"

    if [ -L "$symlink_path" ]; then
        existing_target=$(readlink -f "$symlink_path" 2>/dev/null)
        current_target=$(readlink -f "$iso_path" 2>/dev/null)

        if [ "$existing_target" = "$current_target" ]; then
            skipped=$((skipped + 1))
            continue
        else
            # Collision : même nom, source différente → préfixer avec dossier parent
            parent_dir=$(basename "$(dirname "$iso_path")")
            iso_name="${parent_dir}__${iso_name}"
            symlink_path="$DEST_DIR/$iso_name"
            conflicts=$((conflicts + 1))
            log "[CONFLIT] Nom dupliqué, renommé en : $iso_name"
        fi
    fi

    if [ -L "$symlink_path" ]; then
        rm -f "$symlink_path"
        ln -s "$iso_path" "$symlink_path"
        updated=$((updated + 1))
        log "[MàJ]   $iso_name"
    elif [ -e "$symlink_path" ]; then
        log "[IGNORÉ] Fichier réel existant : $iso_name"
        skipped=$((skipped + 1))
    else
        ln -s "$iso_path" "$symlink_path"
        if [ $? -eq 0 ]; then
            created=$((created + 1))
            log "[CRÉÉ]  $iso_name → $iso_path"
        else
            log "[ERREUR] Impossible de créer : $iso_name"
        fi
    fi

done < <(find "$SOURCE_DIR" -type f -iname "*.iso" \
    -not -path "*/proxmox-view/*" \
    -print0 | sort -z)

# ── Nettoyage des symlinks orphelins ──────────────────────────────────────────
while IFS= read -r -d '' link; do
    if [ -L "$link" ]; then
        target=$(readlink -f "$link" 2>/dev/null)
        if [ -z "$target" ] || [ ! -f "$target" ]; then
            rm -f "$link"
            removed=$((removed + 1))
            log "[SUPPRIMÉ] Orphelin : $(basename "$link")"
        fi
    fi
done < <(find "$DEST_DIR" -maxdepth 1 -name "*.iso" -print0)

log "========================================"
log "Créés: $created | MàJ: $updated | Ignorés: $skipped | Orphelins: $removed | Conflits: $conflicts"
log "========================================"

# Résultat JSON parsable par Python
echo "JSON_RESULT:{\"created\":$created,\"updated\":$updated,\"skipped\":$skipped,\"removed\":$removed,\"conflicts\":$conflicts}"
exit 0
