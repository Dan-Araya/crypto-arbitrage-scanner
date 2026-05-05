#!/usr/bin/env bash
# build_lambdas.sh — Empaqueta todas las Lambdas del directorio lambdas/ en build/.
#
# Uso:
#   ./build_lambdas.sh             # empaqueta todas
#   ./build_lambdas.sh fetch_buda  # empaqueta sólo una
#
# Cada Lambda debe vivir en lambdas/<nombre>/ con un handler.py dentro.
# El zip resultante queda en build/<nombre>.zip y contiene los archivos
# en la raíz (no anidados en un subdirectorio), que es lo que Lambda espera.
#
# Sólo se incluyen archivos .py en el zip: cualquier otra cosa
# (zips sueltos, archivos ocultos, basura editorial) queda excluida.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAMBDAS_DIR="${PROJECT_ROOT}/lambdas"
BUILD_DIR="${PROJECT_ROOT}/build"

mkdir -p "${BUILD_DIR}"

build_one() {
    local name="$1"
    local src_dir="${LAMBDAS_DIR}/${name}"
    local zip_path="${BUILD_DIR}/${name}.zip"

    if [[ ! -d "${src_dir}" ]]; then
        echo "ERROR: ${src_dir} no existe." >&2
        exit 1
    fi

    if [[ ! -f "${src_dir}/handler.py" ]]; then
        echo "ERROR: ${src_dir}/handler.py no existe." >&2
        exit 1
    fi

    rm -f "${zip_path}"

    # Whitelist explícita: incluimos sólo archivos .py.
    # Más seguro que blacklist: si alguien deja un archivo raro en el
    # directorio fuente, no se cuela al zip.
    # Flags:
    #   -X: omite metadata extra (uid/gid) → builds reproducibles
    (
        cd "${src_dir}"
        zip -X "${zip_path}" *.py > /dev/null
    )

    local size_kb
    size_kb=$(du -k "${zip_path}" | cut -f1)
    local file_count
    file_count=$(unzip -l "${zip_path}" | tail -1 | awk '{print $2}')
    echo "✓ ${name} → build/${name}.zip (${size_kb} KB, ${file_count} file(s))"
}

if [[ $# -eq 0 ]]; then
    # Sin argumentos: empaqueta todas las Lambdas
    for lambda_dir in "${LAMBDAS_DIR}"/*/; do
        name=$(basename "${lambda_dir}")
        build_one "${name}"
    done
else
    # Con argumento: empaqueta sólo la indicada
    build_one "$1"
fi