import tensorflow as tf
import numpy as np
from PIL import Image

model_path = 'c:/Users/heman/OralDiagnosisAI-Web/oral_model.tflite'
interpreter = tf.lite.Interpreter(model_path=model_path)
interpreter.allocate_tensors()

input_details = interpreter.get_input_details()
output_details = interpreter.get_output_details()

def predict(image_path):
    print(f"Testing {image_path}")
    image = Image.open(image_path).convert("RGB")
    image = image.resize((224, 224))
    image_array = np.asarray(image, dtype=np.float32) / 255.0
    image_array = np.expand_dims(image_array, axis=0)
    
    interpreter.set_tensor(input_details[0]["index"], image_array)
    interpreter.invoke()
    predictions = interpreter.get_tensor(output_details[0]["index"])
    print(f"Raw outputs: {predictions[0]}")
    print(f"Sum of outputs: {np.sum(predictions[0])}")

# Let's generate a random image and run it.
dummy_image = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
Image.fromarray(dummy_image).save('dummy.png')
predict('dummy.png')
