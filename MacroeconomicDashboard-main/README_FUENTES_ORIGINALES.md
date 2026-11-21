# Fuentes originales y actualización de series

Este paquete recupera las 32 series que el catálogo anterior conservaba sin actualizar: 25 del Banco de España, tres ratios de dudosidad calculados con datos del Banco de España, tres series de la Encuesta Trimestral de Coste Laboral del INE y la variación salarial pactada en convenios del Ministerio de Trabajo. También incorpora conexiones directas para el tipo de depósito del BCE, dos indicadores de confianza de la Comisión Europea y el indicador adelantado de la OCDE.

Las series ETCL66, ETCL67 y ETCL68 se contrastaron con los archivos del INE facilitados: las 70 observaciones trimestrales comunes coinciden. El avance de convenios se comprobó con el libro oficial facilitado: las 21 observaciones mensuales comunes coinciden con diferencias de hasta 0,0051 puntos por redondeo. Desde 2025 se usa el avance mensual del Ministerio; hasta 2024 se conserva el histórico de BDSICE. Los datos recientes de convenios son provisionales.

Las series del Banco de España se cotejaron con los CSV oficiales aportados, incluyendo las conversiones de unidades y los agregados. El BCE, la Comisión Europea y la OCDE se contrastaron con sus muestras respectivas. Cuando falla una descarga o la transición de fuente no supera la comprobación, se conservan los datos anteriores y se registra el error.

**La migración de todas las demás series a sus productores originales sigue pendiente.** Algunas todavía utilizan BDSICE; los indicadores sintéticos de actividad son elaborados por el propio Ministerio de Economía. El histórico de convenios anterior a 2025 también procede de BDSICE.

Las conexiones se han probado con los archivos suministrados, pero no se ha podido verificar desde este entorno el acceso en directo desde GitHub Actions. Tras subir el paquete, ejecuta el flujo de actualización y revisa su registro, en particular la descarga del avance de convenios. El flujo instala `requests`, `pandas`, `python-calamine`, `openpyxl` y `xlrd`.

Este paquete se basa en el ZIP facilitado y no modifica ningún repositorio remoto. Compara con la rama actual antes de subirlo para no sobrescribir otros cambios. El archivo `.github/workflows/update-data.yml` requiere permisos para modificar flujos de trabajo.
