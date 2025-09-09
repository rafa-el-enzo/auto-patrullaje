import subprocess
import time
from pathlib import Path

def test_audio():
    print("Probando reproducción de audio...")
    try:
        alarm_path = str(Path(__file__).parent.parent / 'alarm.mp3')
        print(f"Archivo de audio: {alarm_path}")
        if not Path(alarm_path).exists():
            print("⚠️  ERROR: No se encuentra el archivo de audio")
            return False
            
        subprocess.run([
            'termux-media-player',
            'play',
            alarm_path
        ], check=True)
        print("✅ Audio iniciado")
        
        time.sleep(3)  # Reproducir por 3 segundos
        
        subprocess.run(['termux-media-player', 'stop'], check=True)
        print("✅ Audio detenido correctamente")
        return True
    except Exception as e:
        print(f"❌ Error en audio: {e}")
        return False

def test_notification():
    print("\nProbando notificaciones...")
    try:
        subprocess.run([
            'termux-notification',
            '--title', 'Test Notificación',
            '--content', 'Esta es una prueba de notificación',
            '--priority', 'high',
            '--alert-once'
        ], check=True)
        print("✅ Notificación enviada")
        return True
    except Exception as e:
        print(f"❌ Error en notificación: {e}")
        return False

def test_vibration():
    print("\nProbando vibración...")
    try:
        subprocess.run(['termux-vibrate', '-d', '1000'], check=True)
        print("✅ Vibración ejecutada")
        return True
    except Exception as e:
        print(f"❌ Error en vibración: {e}")
        return False

if __name__ == "__main__":
    print("=== Iniciando pruebas de Termux API ===")
    
    # Verificar que termux-api esté instalado
    try:
        subprocess.run(['termux-api-start'], check=True)
        print("✅ Termux API disponible")
    except Exception as e:
        print(f"❌ Error: Termux API no disponible. ¿Está instalada?\n   {e}")
        print("   Ejecuta: pkg install termux-api")
        exit(1)
    
    # Ejecutar pruebas
    audio_ok = test_audio()
    notif_ok = test_notification()
    vibr_ok = test_vibration()
    
    print("\n=== Resumen de pruebas ===")
    print(f"Audio: {'✅' if audio_ok else '❌'}")
    print(f"Notificaciones: {'✅' if notif_ok else '❌'}")
    print(f"Vibración: {'✅' if vibr_ok else '❌'}")
