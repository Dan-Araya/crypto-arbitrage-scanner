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
#
# Lambdas en LAMBDAS_NEEDING_COMMON reciben además el package lambdas/common/
# embebido en su zip como common/, para que `from common.fx import ...`
# resuelva sin necesidad de Lambda Layer (ver decisión α en notas de diseño).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAMBDAS_DIR="${PROJECT_ROOT}/lambdas"
BUILD_DIR="${PROJECT_ROOT}/build"

# Lambdas que importan from common.*: empaquetamos common/ adentro.
# Alternativa explícita a una Lambda Layer (el módulo es pequeño y solo
# lo usan las Silver Lambdas; layer sería sobre-ingeniería para 50 líneas).
LAMBDAS_NEEDING_COMMON=("silver_binance" "silver_fx")

needs_common() {
    local name="$1"
    for n in "${LAMBDAS_NEEDING_COMMON[@]}"; do
        [[ "$n" == "$name" ]] && return 0
    done
    return 1
}

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

    # Si esta Lambda usa common/, montamos una staging dir con handler.py
    # y common/ adentro, y zipeamos desde ahí. Si no, zipeamos directo
    # desde el directorio de la Lambda (comportamiento original).
    local tmp_build=""
    local pkg_root="${src_dir}"
    if needs_common "${name}"; then
        tmp_build=$(mktemp -d)
        cp "${src_dir}"/*.py "${tmp_build}/"
        mkdir -p "${tmp_build}/common"
        cp "${LAMBDAS_DIR}/common"/*.py "${tmp_build}/common/"
        pkg_root="${tmp_build}"
    fi

    # Whitelist explícita: archivos .py en la raíz + (si aplica) common/.
    # Flags:
    #   -X: omite metadata extra (uid/gid) → builds reproducibles
    #   -r: recursivo (necesario para incluir common/)
    (
        cd "${pkg_root}"
        if needs_common "${name}"; then
            zip -X -r "${zip_path}" *.py common > /dev/null
        else
            zip -X "${zip_path}" *.py > /dev/null
        fi
    )

    local size_kb
    size_kb=$(du -k "${zip_path}" | cut -f1)
    local file_count
    file_count=$(unzip -l "${zip_path}" | tail -1 | awk '{print $2}')
    echo "✓ ${name} → build/${name}.zip (${size_kb} KB, ${file_count} file(s))"

    # Cleanup del staging dir
    [[ -n "${tmp_build}" ]] && rm -rf "${tmp_build}"
}

if [[ $# -eq 0 ]]; then
    # Sin argumentos: empaqueta todas las Lambdas
    for lambda_dir in "${LAMBDAS_DIR}"/*/; do
        name=$(basename "${lambda_dir}")
        # Saltar common/ (no es una Lambda; se incluye dentro de otras)
        [[ "${name}" == "common" ]] && continue
        build_one "${name}"
    done
else
    # Con argumento: empaqueta sólo la indicada
    build_one "$1"
fi
