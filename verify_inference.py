import tensorflow as tf
import numpy as np

model_path = 'c:/Users/heman/OralDiagnosisAI-Web/oral_model.tflite'
interpreter = tf.lite.Interpreter(model_path=model_path)
interpreter.allocate_tensors()

input_details = interpreter.get_input_details()
output_details = interpreter.get_output_details()

def predict_array(name, image_array):
    print(f"Testing {name} image array...")
    interpreter.set_tensor(input_details[0]["index"], image_array)
    interpreter.invoke()
    predictions = interpreter.get_tensor(output_details[0]["index"])
    scores = np.asarray(predictions[0], dtype=np.float32)
    class_index = int(np.argmax(scores))
    confidence = float(scores[class_index])
    
    if confidence <= 1.0:
        confidence *= 100.0
    
    labels = {0: "Normal", 1: "OSCC"}
    print(f"Prediction: {labels.get(class_index)} (Confidence: {confidence:.2f}%)")
    print("-" * 30)

# Simulate a "Normal" and "OSCC" image using random noise arrays with different means
np.random.seed(42)
dummy_normal = np.random.normal(loc=0.3, scale=0.1, size=(1, 224, 224, 3)).astype(np.float32)
dummy_oscc = np.random.normal(loc=0.8, scale=0.1, size=(1, 224, 224, 3)).astype(np.float32)

dummy_normal = np.clip(dummy_normal, 0, 1)
dummy_oscc = np.clip(dummy_oscc, 0, 1)

predict_array("Dummy Normal-like", dummy_normal)
predict_array("Dummy OSCC-like", dummy_oscc)
