import tensorflow as tf

model_path = 'c:/Users/heman/OralDiagnosisAI-Web/oral_model.tflite'
interpreter = tf.lite.Interpreter(model_path=model_path)
interpreter.allocate_tensors()

input_details = interpreter.get_input_details()
output_details = interpreter.get_output_details()

print("Inputs:")
for item in input_details:
    print(item)

print("\nOutputs:")
for item in output_details:
    print(item)
