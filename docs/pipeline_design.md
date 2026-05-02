# Data Quality Considerations

## Solapamiento de un minuto entre archivos consecutivos (Binance)

**Comportamiento observado:** El parámetro `endTime` de Binance es 
inclusivo. Si dos invocaciones consecutivas se hacen con rangos 
[T0, T1] y [T1, T2], la kline con open_time=T1 aparecerá en ambos 
archivos.

**Validado empíricamente:** Una invocación con start_ms=1704067200000 
y end_ms=1704153600000 (1440 minutos) devolvió 1441 records.

**Resolución:** Deduplicación en la capa silver por `open_time` 
como clave natural. La capa bronze se mantiene fiel a la respuesta 
de la API (principio: bronze nunca pierde ni modifica datos de la 
fuente).

**Alternativa descartada:** Restar 1ms al end_ms en el handler. 
Se descartó porque depende de un comportamiento no documentado 
de la API que podría cambiar.